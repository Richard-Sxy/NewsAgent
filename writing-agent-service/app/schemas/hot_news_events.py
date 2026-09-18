"""热点运行的短期 Redis Stream 事件契约。"""

from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


NonBlank128 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
RunKey = Annotated[
    str,
    StringConstraints(pattern=r"^hot-news-[0-9a-f]{64}$"),
]
HotNewsProgressEventName = Literal[
    "hot-news.run.started",
    "hot-news.run.completed",
    "hot-news.run.replayed",
    "hot-news.run.failed",
]
HotNewsProgressStatus = Literal[
    "running",
    "completed",
    "replayed",
    "failed",
]


class HotNewsProgressCounts(BaseModel):
    """只包含已经确定的聚合数量，不携带原始行为记录。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fetched_record_count: int = Field(default=0, ge=0)
    metric_snapshot_count: int = Field(default=0, ge=0)
    ranked_news_count: int = Field(default=0, ge=0)
    analyzed_news_count: int = Field(default=0, ge=0)


class HotNewsProgressEvent(BaseModel):
    """热点运行发送给运营平台的短期进度事件。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: HotNewsProgressEventName
    event_id: str | None = None
    deduplication_key: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
    ]
    tenant_id: NonBlank128
    run_key: RunKey
    run_id: NonBlank128 | None = None
    status: HotNewsProgressStatus
    production_bundle_version: NonBlank128
    workflow_version: NonBlank128
    window_start: AwareDatetime
    window_end: AwareDatetime
    counts: HotNewsProgressCounts = Field(
        default_factory=HotNewsProgressCounts
    )
    error_type: NonBlank128 | None = None
    occurred_at: AwareDatetime

    @model_validator(mode="after")
    def validate_event_state(self) -> Self:
        expected_status = {
            "hot-news.run.started": "running",
            "hot-news.run.completed": "completed",
            "hot-news.run.replayed": "replayed",
            "hot-news.run.failed": "failed",
        }[self.event]
        if self.status != expected_status:
            raise ValueError("event and status do not match")
        if self.window_start >= self.window_end:
            raise ValueError("window_start must be earlier than window_end")
        if self.status in {"completed", "replayed"} and self.run_id is None:
            raise ValueError("completed or replayed event requires run_id")
        if self.status == "failed" and self.error_type is None:
            raise ValueError("failed event requires error_type")
        if self.status != "failed" and self.error_type is not None:
            raise ValueError("error_type is only allowed for failed events")
        return self

