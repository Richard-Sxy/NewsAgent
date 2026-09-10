from functools import lru_cache
from pydantic import Field, PostgresDsn, RedisDsn, SecretStr
from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
)

class Settings(BaseSettings):
    """Writing Agent Service 生产运行配置。"""
    service_name: str = "writing-agent-service"
    environment: str

    database_url: PostgresDsn
    redis_url: RedisDsn
    progress_stream_prefix: str = "news-writing:events"
    progress_stream_maxlen: int = Field(default=2000, ge=100, le=100000)
    progress_dedup_ttl_seconds: int = Field(default=86400, ge=60, le=604800)
    outbox_batch_size: int = Field(default=100, ge=1, le=1000)
    outbox_max_attempts: int = Field(default=12, ge=1, le=100)
    outbox_poll_interval_seconds: float = Field(default=1.0, ge=0.1, le=30)
    sse_block_ms: int = Field(default=15000, ge=1000, le=30000)
    sse_batch_size: int = Field(default=100, ge=1, le=1000)
    sse_retry_ms: int = Field(default=5000, ge=1000, le=30000)

    temporal_address: str
    temporal_namespace: str
    temporal_task_queue: str = "news-writing"
    temporal_hot_news_task_queue: str = "hot-news"
    temporal_data_loop_task_queue: str = "hot-news-data-loop"

    fastgpt_base_url: str
    fastgpt_api_key: str
    fastgpt_dataset_id: str | None = None
    fastgpt_research_app_id: str
    fastgpt_writer_app_id: str
    fastgpt_reviewer_app_id: str
    fastgpt_hot_news_app_id: str | None = None
    fastgpt_error_attribution_app_id: str | None = None
    fastgpt_supervisor_app_id: str | None = None
    # JSON array of complete ProductionBundleSpec snapshots that this worker
    # can actually execute. Data Loop workers fail closed when it is empty or
    # a candidate is not registered.
    hot_news_runtime_manifest_json: str = "[]"
    data_loop_replay_max_concurrency: int = Field(default=8, ge=1, le=32)
    data_loop_max_cases_per_cohort: int = Field(default=20, ge=1, le=100)
    # Shared only with the trusted edge gateway. When absent or blank, every
    # Data Loop management endpoint remains unavailable (fail-closed).
    data_loop_gateway_token: SecretStr | None = None

    artifact_bucket: str
    artifact_endpoint: str | None = None
    artifact_region: str = "us-east-1"
    artifact_prefix: str = "news-writing"
    artifact_sse_algorithm: str = "AES256"
    artifact_kms_key_id: str | None = None

    # 企业 CMS 发布网关。未配置 URL 时发布接口保持关闭，避免误发。
    cms_publish_url: str | None = None
    cms_publish_token: str | None = None
    cms_publish_timeout_seconds: float = Field(default=30.0, ge=1, le=120)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

@lru_cache
def get_settings() -> Settings:
    return Settings()
