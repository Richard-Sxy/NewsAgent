"""热点上游取数的稳定 Port。

编排层只依赖本模块定义的 ``NewsMetricSource``，不关心取数是走参数化模板还是
Text2SQL。返回的必须是确定性窗口指标快照，而不是企业原始用户行为明细。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.analytics.entities import ContentType
from app.analytics.metrics import NewsMetricSnapshot


@dataclass(frozen=True, slots=True)
class HotNewsMetricQuery:
    """一次窗口取数请求；时间使用左闭右开区间 [start, end)。"""

    tenant_id: str
    window_start: datetime
    window_end: datetime
    content_types: frozenset[ContentType] = field(default_factory=frozenset)
    ranking_limit: int = 20
    requested_metrics: frozenset[str] = field(default_factory=frozenset)

    def validate(self) -> None:
        if not self.tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        if (
            self.window_start.tzinfo is None
            or self.window_start.utcoffset() is None
        ):
            raise ValueError("window_start must be timezone-aware")
        if (
            self.window_end.tzinfo is None
            or self.window_end.utcoffset() is None
        ):
            raise ValueError("window_end must be timezone-aware")
        if self.window_start >= self.window_end:
            raise ValueError("window_start must be less than window_end")
        if self.ranking_limit <= 0:
            raise ValueError("ranking_limit must be greater than 0")
        for metric in self.requested_metrics:
            if not metric.strip():
                raise ValueError("requested_metrics cannot contain empty names")


class NewsMetricSource(Protocol):
    """返回确定性窗口指标快照；实现负责租户隔离和只读访问。"""

    async def fetch_snapshots(
        self,
        query: HotNewsMetricQuery,
    ) -> list[NewsMetricSnapshot]:
        ...
