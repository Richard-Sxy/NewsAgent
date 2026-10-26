"""Opt-in telemetry configuration; never load application .env files."""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from uuid import uuid4


class ObservabilitySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OBSERVABILITY_", env_file=None, extra="ignore")

    enabled: bool = False
    service_name: str = "newsagent-api"
    service_version: str = "0.1.0"
    service_instance_id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    environment: str = "local"
    otlp_endpoint: str = "http://127.0.0.1:4318"
    sample_ratio: float = Field(default=1.0, ge=0, le=1)
    json_logs: bool = True
    log_level: str = "INFO"
    export_timeout_seconds: float = Field(default=5, gt=0, le=60)
    metric_export_interval_ms: int = Field(default=15000, ge=1000)

    @field_validator("otlp_endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        from urllib.parse import urlsplit

        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("OTLP endpoint must use HTTP or HTTPS")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("OTLP endpoint must not include credentials, query, or fragment")
        return value.rstrip("/")

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Unsupported telemetry log level")
        return value
