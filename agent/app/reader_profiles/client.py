"""Bounded HTTPS adapter for enterprise-owned reader aggregates only."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from hashlib import sha256
import json
import math
import re
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr, ValidationError

from app.reader_profiles.contracts import (
    ReaderProfileBatch,
    ReaderProfileProviderError,
    ReaderProfileQuery,
    reader_profile_query_fingerprint,
)


_ENVIRONMENTS = frozenset({"local", "development", "test", "e2e", "staging", "production", "prod"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _reject_constant(_value):
    raise ValueError("non-finite JSON")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class HttpNewsReaderProfileClient:
    """One fixed POST, with no redirects, retries, proxies or synthetic fallback."""

    expected_source_mode = "enterprise"

    def __init__(self, *, endpoint_url: str, token: SecretStr,
                 expected_source_release_sha256: str, expected_taxonomy_version: str,
                 expected_profile_policy_version: str, environment: str,
                 timeout_seconds: float = 5, max_response_bytes: int = 262_144,
                 transport: httpx.AsyncBaseTransport | None = None, clock=None) -> None:
        try:
            parts = urlsplit(endpoint_url)
            valid = (type(endpoint_url) is str and parts.scheme == "https" and parts.hostname
                     and parts.port != 0 and parts.username is None and parts.password is None
                     and not parts.query and not parts.fragment
                     and not any(c in endpoint_url for c in ("?", "#", "\\"))
                     and not any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in endpoint_url))
        except (TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise ValueError("Invalid reader profile provider URL configuration")
        if not isinstance(token, SecretStr):
            raise ValueError("Invalid reader profile authentication configuration")
        # The secret is unwrapped only inside this transport boundary.
        secret = token.get_secret_value()
        if (not 1 <= len(secret) <= 4096 or not secret.isascii()
                or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in secret)):
            raise ValueError("Invalid reader profile authentication configuration")
        if (type(expected_source_release_sha256) is not str
                or not _HEX64.fullmatch(expected_source_release_sha256)):
            raise ValueError("Invalid reader profile source release configuration")
        for value in (expected_taxonomy_version, expected_profile_policy_version):
            if (type(value) is not str or not 1 <= len(value) <= 160
                    or any(ord(c) < 33 or ord(c) == 127 for c in value)):
                raise ValueError("Invalid reader profile version configuration")
        if environment not in _ENVIRONMENTS:
            raise ValueError("Invalid reader profile environment configuration")
        if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 120):
            raise ValueError("Invalid reader profile timeout configuration")
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 4_194_304:
            raise ValueError("Invalid reader profile response limit configuration")
        self._url = endpoint_url
        self._token = token
        self.source_release_sha256 = expected_source_release_sha256
        self.expected_taxonomy_version = expected_taxonomy_version
        self.expected_profile_policy_version = expected_profile_policy_version
        self.taxonomy_version = expected_taxonomy_version
        self.profile_policy_version = expected_profile_policy_version
        self.environment = environment
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.clock = clock or (lambda: datetime.now(UTC))
        identity = {"adapter_policy_version": "https-reader-profile-adapter-v1",
                    "endpoint_sha256": sha256(endpoint_url.encode()).hexdigest(),
                    "source_release_sha256": self.source_release_sha256,
                    "taxonomy_version": self.taxonomy_version,
                    "profile_policy_version": self.profile_policy_version,
                    "source_mode": self.expected_source_mode, "environment": environment}
        self._identity_sha256 = sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self._client = httpx.AsyncClient(timeout=timeout_seconds, trust_env=False,
                                         follow_redirects=False, transport=transport)
        self._closed = False

    @property
    def identity_sha256(self) -> str:
        return self._identity_sha256

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._client.aclose()

    async def fetch(self, query: ReaderProfileQuery) -> ReaderProfileBatch:
        if self._closed:
            raise ReaderProfileProviderError("closed")
        try:
            query = ReaderProfileQuery.model_validate(query.model_dump(mode="json"))
        except (ValidationError, AttributeError, TypeError, ValueError):
            raise ReaderProfileProviderError("invalid_query") from None
        if query.source_release_sha256 != self.source_release_sha256:
            raise ReaderProfileProviderError("invalid_query")
        fingerprint = reader_profile_query_fingerprint(query)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with self._client.stream(
                    "POST", self._url, content=query.model_dump_json().encode("utf-8"),
                    headers={"Authorization": "Bearer " + self._token.get_secret_value(),
                             "X-Tenant-ID": str(query.tenant_id), "X-Request-Fingerprint": fingerprint,
                             "Content-Type": "application/json", "Accept": "application/json",
                             "Accept-Encoding": "identity"},
                    follow_redirects=False, timeout=self.timeout_seconds,
                ) as response:
                    self._check_status(response.status_code)
                    if (response.headers.get("content-encoding", "identity").lower() != "identity"
                            or response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                            != "application/json"):
                        raise ReaderProfileProviderError("invalid_response")
                    declared = response.headers.get("content-length")
                    if declared is not None:
                        if not declared.isascii() or not declared.isdigit() or len(declared) > 10:
                            raise ReaderProfileProviderError("invalid_response")
                        if int(declared) > self.max_response_bytes:
                            raise ReaderProfileProviderError("response_too_large")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=4096):
                        if len(raw) + len(chunk) > self.max_response_bytes:
                            raise ReaderProfileProviderError("response_too_large")
                        raw.extend(chunk)
                batch = self._parse_batch(bytes(raw))
                # Service validation is also applied here for callers using the Port directly.
                from app.reader_profiles.service import validate_reader_profile_batch
                validate_reader_profile_batch(query, batch, client=self, now=self.clock())
                return batch
        except ReaderProfileProviderError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise ReaderProfileProviderError("timeout", retryable=True) from None
        except httpx.DecodingError:
            raise ReaderProfileProviderError("invalid_response") from None
        except (httpx.HTTPError, OSError):
            raise ReaderProfileProviderError("unavailable", retryable=True) from None
        except Exception:
            raise ReaderProfileProviderError("unavailable", retryable=True) from None

    @staticmethod
    def _check_status(status: int) -> None:
        if status == 200:
            return
        code = ("redirect" if 300 <= status < 400 else "unauthorized" if status in {401, 403}
                else "rate_limited" if status == 429 else "unavailable" if status >= 500 else "invalid_query")
        raise ReaderProfileProviderError(code, retryable=status == 429 or status >= 500, status_code=status)

    @staticmethod
    def _parse_batch(raw: bytes) -> ReaderProfileBatch:
        try:
            decoded = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant,
                                 object_pairs_hook=_unique_object)
            return ReaderProfileBatch.model_validate(decoded)
        except (ValueError, ValidationError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise ReaderProfileProviderError("invalid_response") from None


HttpReaderProfileClient = HttpNewsReaderProfileClient
