"""Explicit aggregate-provider assembly; validate before creating HTTP resources."""

import math
import re

from pydantic import SecretStr

from app.modules.audience.taxonomy import load_topic_catalog


def _runtime_options(settings):
    environment = settings.environment
    if environment not in {"local", "development", "test", "e2e", "staging", "production", "prod"}:
        raise ValueError("Unsupported reader profile environment")
    if settings.reader_profile_backend not in {"local_package", "enterprise_http"}:
        raise ValueError("Unsupported reader profile backend")
    if (settings.reader_profile_backend == "local_package"
            and environment not in {"local", "development", "test", "e2e"}):
        raise ValueError("Synthetic reader profiles require an isolated local environment")
    for field, minimum, maximum in (
        ("reader_profile_timeout_seconds", 0.1, 30),
        ("reader_profile_run_timeout_seconds", 0.1, 60),
        ("reader_profile_cache_ttl_seconds", 0, 300),
    ):
        value = getattr(settings, field)
        if type(value) not in {int, float} or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError("Invalid reader profile time budget")
    for field, minimum, maximum in (
        ("reader_profile_max_cache_entries", 1, 2048),
        ("reader_profile_max_inflight", 1, 128),
        ("reader_profile_max_response_bytes", 4096, 2_097_152),
    ):
        value = getattr(settings, field)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError("Invalid reader profile resource bound")
    release = settings.reader_profile_source_release_sha256
    if release is not None and (type(release) is not str or not re.fullmatch(r"[0-9a-f]{64}", release)):
        raise ValueError("Invalid reader profile source release")
    policy = settings.reader_profile_policy_version
    if (type(policy) is not str or not 1 <= len(policy) <= 160
            or any(character.isspace() or ord(character) < 33 or ord(character) == 127 for character in policy)):
        raise ValueError("Reader profile policy must be explicitly versioned")
    allowed = settings.reader_profile_allowed_analysis_prompt_versions
    if (type(allowed) is not tuple or not 1 <= len(allowed) <= 32
            or any(type(item) is not str or not 1 <= len(item) <= 160
                   or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in item) for item in allowed)
            or len(set(allowed)) != len(allowed)):
        raise ValueError("Reader-aware analysis Prompt versions must be explicitly configured")
    return frozenset(allowed)


def build_reader_profile_service(settings):
    if not getattr(settings, "reader_profile_enabled", False):
        return None
    # All user-controlled service options are checked before an AsyncClient exists.
    allowed = _runtime_options(settings)
    from app.modules.reader_profiles.service import ReaderProfileService
    if settings.reader_profile_backend == "local_package":
        from app.modules.reader_profiles.local import LocalNewsReaderProfileClient
        if not settings.reader_profile_package_path:
            raise ValueError("READER_PROFILE_PACKAGE_PATH is required")
        client = LocalNewsReaderProfileClient(settings.reader_profile_package_path,
            environment=settings.environment)
        catalog = client.catalog
        if settings.reader_profile_source_release_sha256 and settings.reader_profile_source_release_sha256 != client.source_release_sha256:
            raise ValueError("Local reader package differs from the configured release")
    elif settings.reader_profile_backend == "enterprise_http":
        from app.modules.reader_profiles.client import HttpNewsReaderProfileClient
        if not settings.reader_profile_taxonomy_path or not settings.reader_profile_source_release_sha256:
            raise ValueError("Enterprise reader profiles require a taxonomy and immutable source release")
        if not isinstance(settings.reader_profile_api_token, SecretStr):
            raise ValueError("Enterprise reader profiles require an authentication token")
        catalog = load_topic_catalog(settings.reader_profile_taxonomy_path)
        if catalog.status != "approved":
            raise ValueError("Enterprise reader profiles require an approved taxonomy")
        client = HttpNewsReaderProfileClient(endpoint_url=settings.reader_profile_endpoint_url,
            token=settings.reader_profile_api_token,
            expected_source_release_sha256=settings.reader_profile_source_release_sha256,
            expected_taxonomy_version=catalog.taxonomy_version,
            expected_profile_policy_version=settings.reader_profile_policy_version,
            environment=settings.environment, timeout_seconds=settings.reader_profile_timeout_seconds,
            max_response_bytes=settings.reader_profile_max_response_bytes)
    else:
        raise ValueError("Unsupported reader profile backend")
    expected_mode = "synthetic" if settings.reader_profile_backend == "local_package" else "enterprise"
    if client.expected_source_mode != expected_mode:
        raise ValueError("Reader profile backend returned an unexpected source mode")
    if client.expected_profile_policy_version != settings.reader_profile_policy_version:
        raise ValueError("Reader profile policy differs from the configured version")
    service = ReaderProfileService(client, catalog=catalog, environment=settings.environment,
        timeout_seconds=settings.reader_profile_timeout_seconds,
        cache_ttl_seconds=settings.reader_profile_cache_ttl_seconds,
        max_cache_entries=settings.reader_profile_max_cache_entries,
        max_inflight=settings.reader_profile_max_inflight)
    service.allowed_analysis_prompt_versions = allowed
    service.run_timeout_seconds = settings.reader_profile_run_timeout_seconds
    return service
