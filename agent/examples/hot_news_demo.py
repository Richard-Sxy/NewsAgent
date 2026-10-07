"""使用模拟企业行为数据生成新闻热点排行榜。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.analytics.baseline import BaselineCalculator, NewsMetricBaseline
from app.analytics.entities import BehaviorRecord, ContentType, EventType
from app.analytics.metrics import NewsMetricCalculator, NewsMetricSnapshot
from app.analytics.ranking import HotNewsRanker, RankedHotNews


SCENARIO_PATH = Path(__file__).parent / "data" / "hot_news_scenario.json"


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"window time must include timezone: {value}")
    return parsed


def _expand_window(
    *,
    window_start: datetime,
    news: dict[str, Any],
    metrics: dict[str, int],
    window_name: str,
) -> list[BehaviorRecord]:
    """将紧凑计数展开为分析层实际接收的行为对象。"""

    news_id = str(news["news_id"])
    content_type = ContentType(news["content_type"])
    duration = int(metrics["duration_seconds"])
    records: list[BehaviorRecord] = []

    def append_events(event_type: EventType, count: int, *, seconds: int = 0) -> None:
        for index in range(count):
            records.append(
                BehaviorRecord(
                    event_id=f"{window_name}:{news_id}:{event_type.value}:{index}",
                    user_id=f"user-{index % 50:03d}",
                    news_id=news_id,
                    event_type=event_type,
                    event_time=window_start + timedelta(seconds=index % 3600),
                    content_type=content_type,
                    duration_seconds=seconds,
                    channel=str(news["channel"]),
                    device_type="mobile",
                )
            )

    append_events(EventType.IMPRESSION, int(metrics["impressions"]))
    append_events(EventType.CLICK, int(metrics["clicks"]))
    consumption_event = (
        EventType.READ if content_type == ContentType.ARTICLE else EventType.PLAY
    )
    append_events(
        consumption_event,
        int(metrics["effective_consumptions"]),
        seconds=duration,
    )
    append_events(EventType.LIKE, int(metrics["interactions"]))
    return records


def load_scenario(path: Path = SCENARIO_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_ranking(
    scenario: dict[str, Any],
    *,
    limit: int | None = None,
) -> list[RankedHotNews]:
    """执行行为对象、指标、基线和排行榜完整本地链路。"""

    window_minutes = int(scenario["window_minutes"])
    calculator = NewsMetricCalculator()
    baseline_calculator = BaselineCalculator()

    current_start = _parse_time(scenario["current_window_start"])
    current_end = current_start + timedelta(minutes=window_minutes)
    current_records: list[BehaviorRecord] = []
    historical_by_key: dict[
        tuple[str, ContentType], list[NewsMetricSnapshot]
    ] = {}

    for news in scenario["news"]:
        current_records.extend(
            _expand_window(
                window_start=current_start,
                news=news,
                metrics=news["current"],
                window_name="current",
            )
        )

        content_type = ContentType(news["content_type"])
        key = (str(news["news_id"]), content_type)
        historical_by_key[key] = []
        for index, historical in enumerate(news["historical"]):
            history_start = _parse_time(historical["window_start"])
            history_end = history_start + timedelta(minutes=window_minutes)
            history_records = _expand_window(
                window_start=history_start,
                news=news,
                metrics=historical,
                window_name=f"history-{index}",
            )
            snapshots = calculator.calculate(
                history_records,
                window_start=history_start,
                window_end=history_end,
            )
            if len(snapshots) != 1:
                raise ValueError(f"historical window must produce one snapshot: {key}")
            historical_by_key[key].append(snapshots[0])

    current_snapshots = calculator.calculate(
        current_records,
        window_start=current_start,
        window_end=current_end,
    )
    baselines: dict[tuple[str, ContentType], NewsMetricBaseline] = {
        key: baseline_calculator.calculate(snapshots)
        for key, snapshots in historical_by_key.items()
    }
    return HotNewsRanker().rank(
        current_snapshots,
        baselines,
        limit=limit if limit is not None else int(scenario.get("limit", 10)),
    )


def render_ranking(ranked: list[RankedHotNews], scenario: dict[str, Any]) -> str:
    titles = {str(item["news_id"]): str(item["title"]) for item in scenario["news"]}
    lines = ["模拟企业热点新闻排行榜", "=" * 48]
    for item in ranked:
        current = item.current
        score = item.hot_score
        lines.extend(
            [
                f"{item.rank}. {titles[current.news_id]}",
                f"   news_id: {current.news_id}",
                f"   类型: {current.content_type.value}",
                f"   热点分数: {score.score.quantize(Decimal('0.0000'))}",
                (
                    "   指标: "
                    f"曝光={current.impressions}, 点击={current.clicks}, "
                    f"CTR={current.ctr:.2%}, 有效消费={current.effective_consumptions}, "
                    f"互动={current.interactions}"
                ),
                (
                    "   分量: "
                    f"点击={score.click_component:.4f}, "
                    f"消费={score.consumption_component:.4f}, "
                    f"互动={score.interaction_component:.4f}, "
                    f"增长={score.growth_component:.4f}"
                ),
            ]
        )
    return "\n".join(lines)


def main() -> None:
    scenario = load_scenario()
    print(render_ranking(build_ranking(scenario), scenario))


if __name__ == "__main__":
    main()
