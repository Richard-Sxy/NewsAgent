"""热点 Activity 的低基数监控事件和本地 Prometheus 渲染。"""

from collections import Counter
from dataclasses import dataclass
from typing import Literal, Protocol


RunOutcome = Literal["completed", "replayed", "failed"]


@dataclass(frozen=True, slots=True)
class HotNewsRunMetricEvent:
    outcome: RunOutcome
    error_type: str = "none"
    retryable: bool = False


class HotNewsRunMetrics(Protocol):
    def record(
        self,
        outcome: RunOutcome,
        *,
        error_type: str = "none",
        retryable: bool = False,
    ) -> None: ...


class NoopHotNewsRunMetrics:
    def record(
        self,
        outcome: RunOutcome,
        *,
        error_type: str = "none",
        retryable: bool = False,
    ) -> None:
        return None


class InMemoryHotNewsRunMetrics:
    """本地演练记录器；生产可替换为企业 Metrics SDK Adapter。"""

    def __init__(self) -> None:
        self._events: list[HotNewsRunMetricEvent] = []
        self._counts: Counter[HotNewsRunMetricEvent] = Counter()

    @property
    def events(self) -> tuple[HotNewsRunMetricEvent, ...]:
        return tuple(self._events)

    def record(
        self,
        outcome: RunOutcome,
        *,
        error_type: str = "none",
        retryable: bool = False,
    ) -> None:
        if not error_type or not error_type.replace("_", "").isalnum():
            raise ValueError("error_type must be a low-cardinality identifier")
        event = HotNewsRunMetricEvent(
            outcome=outcome,
            error_type=error_type,
            retryable=retryable,
        )
        self._events.append(event)
        self._counts[event] += 1

    def render_prometheus(self) -> str:
        lines = [
            "# HELP news_agent_hot_news_runs_total Hot news Activity outcomes.",
            "# TYPE news_agent_hot_news_runs_total counter",
        ]
        for event, count in sorted(
            self._counts.items(),
            key=lambda item: (
                item[0].outcome,
                item[0].error_type,
                item[0].retryable,
            ),
        ):
            retryable = str(event.retryable).lower()
            lines.append(
                "news_agent_hot_news_runs_total{"
                f'outcome="{event.outcome}",'
                f'error_type="{event.error_type}",'
                f'retryable="{retryable}"'
                f"}} {count}"
            )
        return "\n".join(lines) + "\n"
