from functools import lru_cache
from typing import Literal
from pydantic import Field, PostgresDsn, RedisDsn, SecretStr, field_validator, model_validator
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
    hot_news_stream_prefix: str = "news-agent:hot-news:events"
    hot_news_stream_maxlen: int = Field(default=1000, ge=100, le=100000)
    hot_news_stream_ttl_seconds: int = Field(
        default=259200,
        ge=60,
        le=2592000,
    )
    hot_news_stream_dedup_ttl_seconds: int = Field(
        default=259200,
        ge=60,
        le=2592000,
    )
    hot_news_stream_publish_timeout_seconds: float = Field(
        default=2.0,
        ge=0.1,
        le=10.0,
    )
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

    model_runtime_backend: Literal["native"] = "native"
    model_runtime_config_path: str | None = None
    # Explicit feature approval. Old enterprise deployments remain unchanged.
    conversation_enabled: bool = False
    conversation_knowledge_enabled: bool = False
    # Only attached by the isolated E2E application; trusted admin is also required.
    conversation_hot_news_query_enabled: bool = False
    conversation_data_analysis_enabled: bool = False
    data_loop_data_analysis_enabled: bool = False
    data_analysis_backend: Literal["process", "docker", "service"] = "process"
    data_analysis_service_url: str | None = None
    data_analysis_service_token: SecretStr | None = None
    data_analysis_service_allow_insecure_http: bool = False
    data_analysis_service_require_os_limits: bool = True
    data_analysis_timeout_seconds: float = Field(default=3, ge=0.1, le=8)
    data_analysis_max_rows: int = Field(default=200, ge=1, le=1000)
    data_analysis_max_input_bytes: int = Field(default=262144, ge=1024, le=1048576)
    data_analysis_max_output_bytes: int = Field(default=262144, ge=1024, le=1048576)
    data_analysis_max_concurrency: int = Field(default=2, ge=1, le=8)
    data_analysis_memory_mb: int = Field(default=128, ge=64, le=512)
    data_analysis_docker_image: str = Field(default="news-agent/data-analysis:local", min_length=1, max_length=200)
    conversation_hot_news_query_scenario: str = Field(default="news-ranking", pattern=r"^[a-z][a-z0-9_-]*$", max_length=64)
    conversation_hot_news_query_hour: int = Field(default=0, ge=0, le=23)
    conversation_context_max_chars: int = Field(default=32000, ge=8000, le=200000)
    conversation_context_recent_chars: int = Field(default=12000, ge=100, le=100000)
    conversation_context_summary_chars: int = Field(default=4000, ge=100, le=8000)
    conversation_context_threshold_ratio: float = Field(default=0.8, gt=0, le=1, allow_inf_nan=False)
    conversation_context_max_compaction_attempts: int = Field(default=2, ge=1, le=4)
    conversation_max_tool_calls: int = Field(default=3, ge=0, le=6)
    conversation_turn_timeout_seconds: float = Field(default=60, ge=5, le=120)
    conversation_stream_heartbeat_seconds: float = Field(default=10, ge=1, le=30)
    conversation_stream_chunk_chars: int = Field(default=128, ge=16, le=1024)
    # 模板优先取数的确定性护栏；模板与模型生成的 SQL 都必须满足这些上限。
    # 表/列白名单与方言由调用方传入的 Text2SqlSchema 定义，不在此处重复配置。
    text2sql_max_rows: int = Field(default=1000, ge=1, le=100000)
    text2sql_timeout_ms: int = Field(default=30000, ge=1000, le=300000)
    sql_assistant_scenarios_path: str = "deploy/text2sql-scenes.local.yml"
    # Only the isolated local SQL application/worker can initialize this fixture.
    sql_assistant_dataset_profile: Literal["classic-v1", "enterprise-v1", "enterprise-v2", "public-headlines-v3"] = "classic-v1"
    sql_assistant_news_per_tenant: Literal[120, 1200, 12000] = 1200
    # JSON array of complete ProductionBundleSpec snapshots that this worker
    # can actually execute. Data Loop workers fail closed when it is empty or
    # a candidate is not registered.
    hot_news_runtime_manifest_json: str = "[]"
    # JSON array of hot-news schedule definitions, one Temporal Schedule per
    # tenant group: [{"tenant_group","tenant_id","production_bundle_version",
    # "window_minutes","interval_minutes","paused"?}]. Empty list disables
    # schedule provisioning; workers run without any scheduled triggers.
    hot_news_schedule_definitions_json: str = "[]"
    hot_news_dependencies_factory: str | None = None
    # Per-worker online analysis limit, shared across tenants/windows. One keeps
    # existing deployments serial; isolated demo explicitly opts into three.
    hot_news_analysis_max_concurrency: int = Field(default=1, ge=1, le=16)
    data_loop_replay_max_concurrency: int = Field(default=8, ge=1, le=32)
    data_loop_max_cases_per_cohort: int = Field(default=20, ge=1, le=100)
    # Shared only with the trusted edge gateway. When absent or blank, every
    # Data Loop management endpoint remains unavailable (fail-closed).
    data_loop_gateway_token: SecretStr | None = None
    knowledge_ingest_token: SecretStr | None = None

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

    @field_validator("sql_assistant_news_per_tenant", mode="before")
    @classmethod
    def parse_synthetic_news_count(cls, value):
        # Environment values are strings. Convert only the three exact approved
        # spellings; bool, floats, whitespace and alternative numeric forms must
        # not silently select a different fixture identity.
        approved = {"120": 120, "1200": 1200, "12000": 12000}
        if type(value) is str and value in approved:
            return approved[value]
        if type(value) is int and value in approved.values():
            return value
        raise ValueError("synthetic dataset news count must be exactly 120, 1200 or 12000")

    @model_validator(mode="after")
    def require_native_model_runtime(self) -> "Settings":
        if self.conversation_context_recent_chars + 2 * self.conversation_context_summary_chars + 1000 >= int(
            self.conversation_context_max_chars * self.conversation_context_threshold_ratio
        ):
            raise ValueError("Conversation context must reserve space for summary, references and current input")
        if not self.model_runtime_config_path:
            raise ValueError("Python model runtime requires MODEL_RUNTIME_CONFIG_PATH")
        analysis_enabled = self.conversation_data_analysis_enabled or self.data_loop_data_analysis_enabled
        if (analysis_enabled and self.data_analysis_backend == "process"
                and self.environment not in {"local", "development", "test", "e2e"}):
            raise ValueError("Process analysis isolation is limited to local/development/test/e2e; configure docker for production")
        if analysis_enabled and self.data_analysis_backend == "service":
            if not self.data_analysis_service_url or self.data_analysis_service_token is None:
                raise ValueError("Service analysis requires DATA_ANALYSIS_SERVICE_URL and DATA_ANALYSIS_SERVICE_TOKEN")
            secret = self.data_analysis_service_token.get_secret_value()
            if not (16 <= len(secret) <= 4096 and secret.isascii() and not any(char.isspace() for char in secret)):
                raise ValueError("Analysis service token must be a bounded nonblank ASCII secret")
            if self.environment not in {"local", "development", "test", "e2e"} and (
                self.data_analysis_service_allow_insecure_http or not self.data_analysis_service_require_os_limits
            ):
                raise ValueError("Production analysis service requires TLS and enforced OS resource limits")
        return self

@lru_cache
def get_settings() -> Settings:
    return Settings()
