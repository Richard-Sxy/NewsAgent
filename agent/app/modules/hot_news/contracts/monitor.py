"""Small, tenant-scoped projection for the chat discovery panel."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")

class MonitorStart(Contract):
    window_minutes: Literal[5, 15]

class MonitorSwitch(Contract):
    enabled: StrictBool

class MonitorView(Contract):
    monitor_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    window_minutes: Literal[5, 15]
    enabled: bool
    status: Literal["waiting_source", "published", "paused", "blocked", "backpressure"]
    source_end_ms: int | None = None
    error_type: str | None = Field(default=None, max_length=100)

class HotNewsNotice(Contract):
    # Stable across overlapping windows; updates replace the same card.
    notice_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    monitor_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    news_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    window_start_ms: int = Field(gt=0)
    window_end_ms: int = Field(gt=0)
    expires_at_ms: int = Field(gt=0)
    hot_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    clicks: int = Field(ge=0)
    unique_users: int = Field(ge=0)
    trend_assessment: str = Field(min_length=1, max_length=1500)
    policy_version: Literal["hot-discovery-v2"] = "hot-discovery-v2"

    @model_validator(mode="after")
    def valid_window(self):
        if (self.window_start_ms % 60000 or
                self.window_end_ms - self.window_start_ms not in (300000, 900000) or
                self.expires_at_ms != self.window_end_ms + 300000 or
                self.unique_users > self.clicks):
            raise ValueError("Notice has inconsistent window/metrics")
        return self

class MonitorFeed(Contract):
    configured: bool = True
    can_manage: bool = False
    tenant_id: str
    scope: Literal["isolated_synthetic"] = "isolated_synthetic"
    source_state: Literal["ready", "waiting_source", "disabled", "unavailable"] = "ready"
    monitors: list[MonitorView] = Field(default_factory=list, max_length=100)
    items: list[HotNewsNotice] = Field(default_factory=list, max_length=3)

