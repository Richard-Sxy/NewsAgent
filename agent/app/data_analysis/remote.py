"""连接福利分析服务。RemoteAnalysisRunner 通过 HTTP 发送数据集，处理健全、并发与超时，并检查结果及输入哈希是否匹配。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from urllib.parse import urlsplit

import httpx

from .engine import AnalysisInputError, validate_request
from .runner import (
    AnalysisExecutionError, AnalysisRunner, _json_bytes, _reject_constant,
    _unique_object,
)


class RemoteAnalysisRunner:
    def __init__(
        self, *, url: str, token: str, timeout_seconds: float = 5,
        max_rows: int = 200, max_input_bytes: int = 262_144,
        max_output_bytes: int = 262_144, max_concurrency: int = 2,
        memory_mb: int = 128, allow_insecure_http: bool = False,
        require_os_limits: bool = True, client: httpx.AsyncClient | None = None,
    ) -> None:
        # Reuse the local contract's strict validation of finite, bounded limits.
        self._validator = AnalysisRunner(
            timeout_seconds=timeout_seconds, max_rows=max_rows,
            max_input_bytes=max_input_bytes, max_output_bytes=max_output_bytes,
            max_concurrency=max_concurrency, memory_mb=memory_mb,
        )
        try:
            parsed = urlsplit(url)
            valid = (
                type(url) is str and "\\" not in url and not any(character.isspace() for character in url)
                and parsed.scheme in ({"https", "http"} if allow_insecure_http else {"https"})
                and parsed.hostname and parsed.username is None and parsed.password is None
                and not parsed.query and not parsed.fragment and "?" not in url and "#" not in url
                and parsed.path == "/v1/analyze" and parsed.port != 0
                and not any(ord(character) < 33 or ord(character) == 127 for character in url)
            )
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise ValueError("Invalid analysis service URL configuration")
        if (type(token) is not str or not 16 <= len(token) <= 4096
                or not token.isascii() or any(character.isspace() or ord(character) < 33
                                             or ord(character) == 127 for character in token)):
            raise ValueError("Invalid analysis service authentication configuration")
        if type(allow_insecure_http) is not bool or type(require_os_limits) is not bool:
            raise ValueError("Invalid analysis transport policy")
        self._url = url
        self._token = token
        self.timeout_seconds = timeout_seconds
        self.max_rows = max_rows
        self.max_input_bytes = max_input_bytes
        self.max_output_bytes = max_output_bytes
        self.max_concurrency = max_concurrency
        self.memory_mb = memory_mb
        self.require_os_limits = require_os_limits
        self._client = client or httpx.AsyncClient(
            trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds),
            limits=httpx.Limits(max_connections=max_concurrency, max_keepalive_connections=max_concurrency),
        )
        self._owns_client = client is None
        self._occupied = 0
        self._closed = False

    def description(self) -> dict:
        limits = dict(self._validator.description()["resource_limits"])
        # This describes the required service contract, not the API host's OS.
        limits["os_resource_limits_enforced"] = self.require_os_limits
        return {"execution_backend": "service", "resource_limits": limits}

    async def close(self) -> None:
        self._closed = True
        if self._owns_client:
            await self._client.aclose()

    async def run(self, request: dict) -> dict:
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        try:
            if len(_json_bytes(request)) > self.max_input_bytes:
                raise AnalysisExecutionError("input_limit")
            normalized = validate_request(request)
            total_rows = sum(
                len(normalized[key]["rows"])
                for key in ("dataset", "reference_dataset") if key in normalized
            )
            if total_rows > self.max_rows:
                raise AnalysisExecutionError("row_limit")
            payload = _json_bytes(normalized)
        except (AnalysisInputError, ValueError, TypeError, OverflowError, RecursionError):
            raise AnalysisExecutionError("invalid_request") from None
        if len(payload) > self.max_input_bytes:
            raise AnalysisExecutionError("input_limit")
        if self._closed:
            raise AnalysisExecutionError("service_unavailable")
        if self._occupied >= self.max_concurrency:
            raise AnalysisExecutionError("service_busy")
        self._occupied += 1
        try:
            async with asyncio.timeout_at(deadline):
                async with self._client.stream(
                    "POST", self._url, content=payload,
                    headers={"Authorization": "Bearer " + self._token,
                             "Content-Type": "application/json", "Accept": "application/json",
                             "Accept-Encoding": "identity"},
                    follow_redirects=False, timeout=self.timeout_seconds,
                ) as response:
                    if response.status_code != 200:
                        code = {
                            401: "service_unauthorized", 403: "service_unauthorized",
                            413: "input_limit", 429: "service_busy", 504: "timeout",
                        }.get(response.status_code, "service_failed")
                        raise AnalysisExecutionError(code)
                    # The fixed service emits uncompressed JSON. Reject encoded
                    # bodies before httpx can expand a compressed response bomb.
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise AnalysisExecutionError("invalid_output")
                    declared = response.headers.get("content-length")
                    if declared is not None and (
                        not declared.isascii() or not declared.isdigit() or len(declared) > 10
                    ):
                        raise AnalysisExecutionError("invalid_output")
                    if declared is not None and int(declared) > self.max_output_bytes:
                        raise AnalysisExecutionError("output_limit")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=4096):
                        if len(raw) + len(chunk) > self.max_output_bytes:
                            raise AnalysisExecutionError("output_limit")
                        raw.extend(chunk)
                result = self._validate_response(bytes(raw), normalized, payload)
                if asyncio.get_running_loop().time() >= deadline:
                    raise AnalysisExecutionError("timeout")
                return result
        except (TimeoutError, httpx.TimeoutException):
            raise AnalysisExecutionError("timeout") from None
        except (httpx.HTTPError, OSError):
            raise AnalysisExecutionError("service_unavailable") from None
        except AnalysisExecutionError:
            raise
        except Exception:
            raise AnalysisExecutionError("service_failed") from None
        finally:
            self._occupied -= 1

    def _validate_response(self, raw: bytes, request: dict, payload: bytes) -> dict:
        try:
            report = json.loads(
                raw.decode("utf-8"), parse_constant=_reject_constant,
                object_pairs_hook=_unique_object,
            )
            if type(report) is not dict:
                raise ValueError
            execution = report.get("execution")
            if (type(execution) is not dict or set(execution) != {
                    "backend", "elapsed_ms", "input_sha256", "limits"}
                    or execution["backend"] not in {"process", "docker"}
                    or type(execution["elapsed_ms"]) is not int
                    or not 0 <= execution["elapsed_ms"] <= 60_000
                    or type(execution["input_sha256"]) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", execution["input_sha256"]) is None
                    or execution["input_sha256"] != hashlib.sha256(payload).hexdigest()):
                raise ValueError
            self._validate_limits(execution["limits"], request, payload)
            if len(raw) > execution["limits"]["max_output_bytes"]:
                # A valid-looking report cannot claim a server output ceiling
                # smaller than the actual wire payload recorded by this Port.
                raise ValueError
            core = {key: value for key, value in report.items() if key != "execution"}
            result = self._validator._validate_output(_json_bytes(core), request)
            result["execution"] = {**execution, "transport": "service"}
            return result
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError):
            raise AnalysisExecutionError("invalid_output") from None

    def _validate_limits(self, limits: object, request: dict, payload: bytes) -> None:
        expected = self.description()["resource_limits"]
        if type(limits) is not dict or set(limits) != set(expected):
            raise ValueError
        timeout = limits["timeout_seconds"]
        if (type(timeout) not in {int, float} or not math.isfinite(timeout)
                or not 0 < timeout <= self.timeout_seconds):
            raise ValueError
        for key, minimum in (
            ("max_rows", 1), ("max_input_bytes", 1), ("max_output_bytes", 1),
            ("max_concurrency", 1), ("memory_mb", 32),
        ):
            if type(limits[key]) is not int or not minimum <= limits[key] <= expected[key]:
                raise ValueError
        rows = sum(len(request[key]["rows"]) for key in ("dataset", "reference_dataset") if key in request)
        if limits["max_rows"] < rows or limits["max_input_bytes"] < len(payload):
            raise ValueError
        if type(limits["os_resource_limits_enforced"]) is not bool:
            raise ValueError
        if self.require_os_limits and not limits["os_resource_limits_enforced"]:
            raise ValueError
