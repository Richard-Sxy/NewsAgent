"""热点分析端到端联调所需的本地依赖装配。

该模块只读取仓库内的模拟场景数据，不连接或保存企业原始行为明细。
Temporal、PostgreSQL、FastGPT 和 FastGPT 知识库仍使用真实外部服务。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.analytics.baseline import BaselineCalculator, NewsMetricBaseline
from app.analytics.data_source import InMemoryBehaviorDataSource
from app.analytics.entities import BehaviorRecord, ContentType, EventType
from app.analytics.metrics import NewsMetricCalculator, NewsMetricSnapshot
from app.analytics.news_content import InMemoryNewsContentRepository, NewsContent
from app.services.hot_news_orchestration import (
    HotNewsBaselineProvider,
    HotNewsOrchestrationPolicy,
)
from examples.hot_news_demo import load_scenario


SCENARIO_PATH = Path(__file__).parent / "data" / "hot_news_scenario.json"
E2E_TENANT_ID = "local-e2e"
E2E_PRODUCTION_BUNDLE_VERSION = "bundle-v1"

MetricKey = tuple[str, ContentType]


@dataclass(frozen=True, slots=True)
class HotNewsE2EDependencies:
    """Worker 依赖和启动 Workflow 时需要复用的稳定参数。"""

    behavior_data_source: InMemoryBehaviorDataSource
    baseline_provider: HotNewsBaselineProvider
    content_repository: InMemoryNewsContentRepository
    policy: HotNewsOrchestrationPolicy
    tenant_id: str
    window_start: datetime
    window_end: datetime
    production_bundle_version: str


class ScenarioHotNewsBaselineProvider:
    """从场景文件预先计算的历史窗口中读取确定性基线。"""

    def __init__(
        self,
        *,
        baselines: dict[MetricKey, NewsMetricBaseline],
        window_start: datetime,
        window_end: datetime,
        production_bundle_version: str,
    ) -> None:
        self._baselines = dict(baselines)
        self._window_start = window_start
        self._window_end = window_end
        self._production_bundle_version = production_bundle_version

    async def get_baselines(
        self,
        *,
        tenant_id: str,
        window_start: datetime,
        window_end: datetime,
        production_bundle_version: str,
        metric_keys: frozenset[MetricKey],
    ) -> dict[MetricKey, NewsMetricBaseline]:
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        if window_start != self._window_start or window_end != self._window_end:
            raise ValueError("E2E request window does not match scenario window")
        if production_bundle_version != self._production_bundle_version:
            raise ValueError(
                "E2E request production bundle does not match loaded scenario"
            )

        return {
            key: self._baselines[key]
            for key in metric_keys
            if key in self._baselines
        }


def _parse_time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"scenario time must include timezone: {value}")
    return parsed


def _expand_behavior_records(
    *,
    news: dict[str, Any],
    metrics: dict[str, Any],
    window_start: datetime,
    window_minutes: int,
    window_name: str,
) -> list[BehaviorRecord]:
    news_id = str(news["news_id"])
    content_type = ContentType(str(news["content_type"]))
    duration_seconds = int(metrics["duration_seconds"])
    window_seconds = window_minutes * 60
    records: list[BehaviorRecord] = []

    def append_events(
        event_type: EventType,
        count: int,
        *,
        consumption_seconds: int = 0,
    ) -> None:
        for index in range(count):
            records.append(
                BehaviorRecord(
                    event_id=(
                        f"{window_name}:{news_id}:{event_type.value}:{index}"
                    ),
                    user_id=f"e2e-user-{index % 50:03d}",
                    news_id=news_id,
                    event_type=event_type,
                    event_time=window_start
                    + timedelta(seconds=index % window_seconds),
                    content_type=content_type,
                    duration_seconds=consumption_seconds,
                    channel=str(news.get("channel") or "unknown"),
                    device_type="mobile",
                )
            )

    append_events(EventType.IMPRESSION, int(metrics["impressions"]))
    append_events(EventType.CLICK, int(metrics["clicks"]))
    append_events(
        EventType.READ if content_type == ContentType.ARTICLE else EventType.PLAY,
        int(metrics["effective_consumptions"]),
        consumption_seconds=duration_seconds,
    )
    append_events(EventType.LIKE, int(metrics["interactions"]))
    return records


def _build_baseline(
    *,
    news: dict[str, Any],
    window_minutes: int,
    metric_calculator: NewsMetricCalculator,
    baseline_calculator: BaselineCalculator,
) -> NewsMetricBaseline:
    snapshots: list[NewsMetricSnapshot] = []
    for index, historical in enumerate(news["historical"]):
        historical_start = _parse_time(historical["window_start"])
        historical_end = historical_start + timedelta(minutes=window_minutes)
        records = _expand_behavior_records(
            news=news,
            metrics=historical,
            window_start=historical_start,
            window_minutes=window_minutes,
            window_name=f"history-{index}",
        )
        calculated = metric_calculator.calculate(
            records,
            window_start=historical_start,
            window_end=historical_end,
        )
        if len(calculated) != 1:
            raise ValueError(
                "each E2E historical window must produce exactly one snapshot: "
                f"news_id={news['news_id']!r}"
            )
        snapshots.append(calculated[0])

    return baseline_calculator.calculate(snapshots)


def build_hot_news_e2e_dependencies(
    scenario_path: Path = SCENARIO_PATH,
) -> HotNewsE2EDependencies:
    """把紧凑场景 JSON 装配为热点 Worker 所需的四类依赖。"""

    scenario = load_scenario(scenario_path)
    window_minutes = int(scenario["window_minutes"])
    if window_minutes <= 0:
        raise ValueError("window_minutes must be greater than 0")

    window_start = _parse_time(scenario["current_window_start"])
    window_end = window_start + timedelta(minutes=window_minutes)
    metric_calculator = NewsMetricCalculator()
    baseline_calculator = BaselineCalculator()

    current_records: list[BehaviorRecord] = []
    contents: list[NewsContent] = []
    baselines: dict[MetricKey, NewsMetricBaseline] = {}
    content_types: set[ContentType] = set()

    for news in scenario["news"]:
        content_type = ContentType(str(news["content_type"]))
        content_types.add(content_type)
        current_records.extend(
            _expand_behavior_records(
                news=news,
                metrics=news["current"],
                window_start=window_start,
                window_minutes=window_minutes,
                window_name="current",
            )
        )

        baseline = _build_baseline(
            news=news,
            window_minutes=window_minutes,
            metric_calculator=metric_calculator,
            baseline_calculator=baseline_calculator,
        )
        baselines[(baseline.news_id, baseline.content_type)] = baseline

        summary = str(news["summary"]).strip()
        contents.append(
            NewsContent(
                news_id=str(news["news_id"]),
                title=str(news["title"]),
                summary=summary,
                body=summary,
                content_type=content_type,
                publish_time=_parse_time(news["publish_time"]),
                source_url=str(news["source_url"]),
            )
        )

    policy = HotNewsOrchestrationPolicy(
        production_bundle_version=E2E_PRODUCTION_BUNDLE_VERSION,
        content_types=frozenset(content_types),
        ranking_limit=int(scenario.get("limit", len(contents))),
        related_limit=3,
        candidate_limit=20,
    )
    policy.validate()

    return HotNewsE2EDependencies(
        behavior_data_source=InMemoryBehaviorDataSource(current_records),
        baseline_provider=ScenarioHotNewsBaselineProvider(
            baselines=baselines,
            window_start=window_start,
            window_end=window_end,
            production_bundle_version=E2E_PRODUCTION_BUNDLE_VERSION,
        ),
        content_repository=InMemoryNewsContentRepository(contents),
        policy=policy,
        tenant_id=E2E_TENANT_ID,
        window_start=window_start,
        window_end=window_end,
        production_bundle_version=E2E_PRODUCTION_BUNDLE_VERSION,
    )
