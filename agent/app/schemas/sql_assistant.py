"""交互式查询数据：首先查询意图、SQL浏览、执行结果、工具轨迹。"""

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


SqlMetric = Literal["clicks", "ctr", "interactions", "impressions", "hot_score"]


class SqlAssistantIntent(BaseModel):
    """A model may select approved semantics, never tenant identities or SQL text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sort_by: SqlMetric
    sort_direction: Literal["asc", "desc"] = "desc"
    content_type: Literal["article", "video"] | None = None
    category: str | None = Field(default=None, max_length=40)
    row_limit: int = Field(default=10, ge=1, le=1000)
    explanation: str = Field(min_length=1, max_length=1000)


class SqlAssistantPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=2, max_length=1000)
    scenario_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    window_start: AwareDatetime
    window_end: AwareDatetime

    @model_validator(mode="after")
    def bounded_window(self) -> "SqlAssistantPreviewRequest":
        if not self.question.strip():
            raise ValueError("请输入查询问题")
        seconds = (self.window_end - self.window_start).total_seconds()
        if not 0 < seconds <= 7 * 86400:
            raise ValueError("查询窗口必须大于零且不超过7天")
        if any(value.minute or value.second or value.microsecond for value in (self.window_start, self.window_end)):
            raise ValueError("小时聚合数据的查询起止时间必须对齐整点")
        return self


class SqlAssistantStage(BaseModel):
    name: str
    status: Literal["passed", "blocked", "degraded"] = "passed"
    detail: str
    elapsed_ms: int = 0
    attempts: int = 1


class SqlAssistantPreview(BaseModel):
    query_id: str
    question: str
    scenario_id: str
    sql: str
    parameters: dict[str, str | int]
    sql_hash: str
    schema_version: str
    schema_sha256: str
    explanation: str
    model_request_id: str | None
    model_provider: str
    expires_at: datetime
    stages: list[SqlAssistantStage]


class SqlAssistantResult(BaseModel):
    query_id: str
    columns: list[str]
    rows: list[dict[str, str | int | float | None]]
    row_count: int
    elapsed_ms: int
    truncated: bool
    summary: str
    stages: list[SqlAssistantStage]
    sql_hash: str


class HotNewsSqlToolTrace(BaseModel):
    """Read-only projection of the SQL tool inside one completed hot-news run."""

    model_config = ConfigDict(extra="forbid")
    query_id: str
    preview: SqlAssistantPreview
    result: SqlAssistantResult
    scope_sha256: str | None = None
    supplemental_sql: str | None = None
    supplemental_sql_hash: str | None = None
    metric_mapping_notes: list[str] = Field(default_factory=list)
