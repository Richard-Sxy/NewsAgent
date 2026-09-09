"""可替换的热点运行可观测性接口。"""

from app.observability.hot_news import (
    HotNewsRunMetricEvent,
    HotNewsRunMetrics,
    InMemoryHotNewsRunMetrics,
    NoopHotNewsRunMetrics,
)

__all__ = [
    "HotNewsRunMetricEvent",
    "HotNewsRunMetrics",
    "InMemoryHotNewsRunMetrics",
    "NoopHotNewsRunMetrics",
]
