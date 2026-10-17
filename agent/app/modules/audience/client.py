"""One bounded, read-only HTTP call to the existing enterprise audience service."""

from __future__ import annotations

import asyncio
import json
import math
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from app.modules.audience.contracts import (
    AudienceProviderError,
    AudienceQuery,
    AudienceSnapshot,
    audience_query_fingerprint,
)


def _reject_constant(_value):
    raise ValueError("non-finite JSON")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class HttpAudienceProfileClient:
    def __init__(self, *, endpoint_url: str, token: str, timeout_seconds: float = 5,
                 max_response_bytes: int = 262_144, allow_insecure_http: bool = False,
                 client: httpx.AsyncClient | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        try:
            parts = urlsplit(endpoint_url)
            valid = (type(endpoint_url) is str and parts.hostname and parts.port != 0
                     and parts.scheme in ({"https", "http"} if allow_insecure_http else {"https"})
                     and parts.username is None and parts.password is None
                     and not parts.query and not parts.fragment
                     and "?" not in endpoint_url and "#" not in endpoint_url and "\\" not in endpoint_url
                     and not any(character.isspace() or ord(character) < 33 or ord(character) == 127
                                 for character in endpoint_url))
        except (TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise ValueError("Invalid audience provider URL configuration")
        if (type(token) is not str or not 1 <= len(token) <= 4096 or not token.isascii()
                or any(character.isspace() or ord(character) < 33 or ord(character) == 127
                       for character in token)):
            raise ValueError("Invalid audience provider authentication configuration")
        if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 120):
            raise ValueError("Invalid audience provider timeout configuration")
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 4_194_304:
            raise ValueError("Invalid audience provider response limit configuration")
        if type(allow_insecure_http) is not bool or (client is not None and transport is not None):
            raise ValueError("Invalid audience provider transport configuration")
        self._url = endpoint_url
        self._token = token
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds, trust_env=False, follow_redirects=False, transport=transport)
        self._owns_client = client is None
        self._closed = False

    async def close(self) -> None:
        self._closed = True
        if self._owns_client:
            await self._client.aclose()

    async def estimate(self, query: AudienceQuery) -> AudienceSnapshot:
        if self._closed:
            raise AudienceProviderError("closed")
        fingerprint = audience_query_fingerprint(query)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with self._client.stream(
                    "POST", self._url, content=query.model_dump_json().encode("utf-8"),
                    headers={"Authorization": "Bearer " + self._token,
                             "X-Tenant-ID": str(query.tenant_id), "X-Request-Fingerprint": fingerprint,
                             "Content-Type": "application/json", "Accept": "application/json",
                             "Accept-Encoding": "identity"},
                    follow_redirects=False, timeout=self.timeout_seconds,
                ) as response:
                    self._check_status(response.status_code)
                    if (response.headers.get("content-encoding", "identity").lower() != "identity"
                            or response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                            != "application/json"):
                        raise AudienceProviderError("invalid_response")
                    declared = response.headers.get("content-length")
                    if declared is not None:
                        if not declared.isascii() or not declared.isdigit() or len(declared) > 10:
                            raise AudienceProviderError("invalid_response")
                        if int(declared) > self.max_response_bytes:
                            raise AudienceProviderError("response_too_large")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=4096):
                        if len(raw) + len(chunk) > self.max_response_bytes:
                            raise AudienceProviderError("response_too_large")
                        raw.extend(chunk)
                snapshot = self._parse_snapshot(bytes(raw))
                if (snapshot.tenant_id != query.tenant_id or snapshot.run_id != query.run_id
                        or snapshot.news_id != query.news_id
                        or snapshot.taxonomy_version != query.selection.taxonomy_version
                        or snapshot.window_start != query.window_start or snapshot.window_end != query.window_end
                        or snapshot.query_fingerprint != fingerprint or snapshot.source_mode != "enterprise"):
                    raise AudienceProviderError("invalid_response")
                return snapshot
        except AudienceProviderError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise AudienceProviderError("timeout", retryable=True) from None
        except httpx.DecodingError:
            raise AudienceProviderError("invalid_response") from None
        except (httpx.HTTPError, OSError):
            raise AudienceProviderError("unavailable", retryable=True) from None

    @staticmethod
    def _check_status(status: int) -> None:
        if status == 200:
            return
        code = ("redirect" if 300 <= status < 400 else "unauthorized" if status in {401, 403}
                else "rate_limited" if status == 429 else "unavailable" if status >= 500 else "rejected")
        raise AudienceProviderError(code, retryable=status == 429 or status >= 500, status_code=status)

    @staticmethod
    def _parse_snapshot(raw: bytes) -> AudienceSnapshot:
        try:
            decoded = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant,
                                 object_pairs_hook=_unique_object)
            return AudienceSnapshot.model_validate(decoded)
        except (ValueError, ValidationError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise AudienceProviderError("invalid_response") from None
