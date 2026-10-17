"""Explicit audience dependency selection; production never falls back to fixtures."""

from app.audience.client import HttpAudienceProfileClient
from app.audience.contracts import AudienceProfilePort
from app.audience.local import LocalAudienceProfileClient


def build_audience_client(settings, catalog, *, behavior_runtime=None) -> AudienceProfilePort:
    if settings.audience_backend == "behavior_package":
        from app.audience.behavior_package import BehaviorPackageAudienceClient
        if behavior_runtime is None:
            raise ValueError("Behavior package audiences require enabled behavior tags")
        if catalog != behavior_runtime.policy.catalog:
            raise ValueError("Behavior package audience taxonomy mismatch")
        return BehaviorPackageAudienceClient(behavior_runtime,
            ttl_seconds=min(300, settings.audience_max_snapshot_age_seconds))
    if settings.audience_backend == "synthetic":
        return LocalAudienceProfileClient(catalog, environment=settings.environment,
            ttl_seconds=min(300, getattr(settings, "audience_max_snapshot_age_seconds", 900)))
    if settings.audience_backend != "enterprise_http":
        raise ValueError("Unsupported audience backend configuration")
    if catalog.status != "approved":
        raise ValueError("Enterprise audiences require an approved taxonomy catalog")
    configured_token = settings.audience_api_token
    token = configured_token.get_secret_value() if configured_token is not None else ""
    return HttpAudienceProfileClient(
        endpoint_url=settings.audience_estimate_url,
        token=token,
        timeout_seconds=settings.audience_timeout_seconds,
        max_response_bytes=settings.audience_max_response_bytes,
    )
