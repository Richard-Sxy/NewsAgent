"""按新闻和时间窗口聚合用户行为指标。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, TypedDict

from app.analytics.entities import BehaviorRecord, ContentType, EventType


class NewsMetricReference(Protocol):
    """热点计算所需的最小指标接口。

    当前窗口快照使用整数计数，历史基线可以使用多个窗口的 Decimal 均值；
    热点计算器只依赖这些公共字段。
    """

    news_id: str
    content_type: ContentType
    clicks: int | Decimal
    effective_consumptions: int | Decimal
    interactions: int | Decimal


class _MetricAccumulator(TypedDict):
    impressions: int
    clicks: int
    user_ids: set[str]
    total_duration_seconds: int
    effective_consumptions: int
    interactions: int


@dataclass(frozen=True, slots=True)       # 不可变字段/字段固定
class NewsMetricSnapshot:
    """一篇新闻在一个固定窗口内的确定性指标快照。"""

    news_id: str
    content_type: ContentType
    window_start: datetime
    window_end: datetime
    impressions: int
    clicks: int
    unique_users: int
    total_duration_seconds: int
    effective_consumptions: int
    interactions: int
    ctr: Decimal


class NewsMetricCalculator:
    """将企业查询得到的 Python 对象聚合为新闻指标。"""

    def __init__(
        self,
        *,
        article_effective_seconds: int = 20,
        video_effective_seconds: int = 10,
    ) -> None:
        if article_effective_seconds < 0 or video_effective_seconds < 0:
            raise ValueError("有效消费时长阈值不能小于 0")
        self.article_effective_seconds = article_effective_seconds
        self.video_effective_seconds = video_effective_seconds

    def calculate(
        self,
        records: list[BehaviorRecord],
        *,
        window_start: datetime,
        window_end: datetime,
        source_guarantees_unique_event_id: bool = False,
    ) -> list[NewsMetricSnapshot]:
        """按 `(news_id, content_type)` 聚合窗口内数据。

        互动事件第一版包括 LIKE、COMMENT、SHARE、FAVORITE。这里统计事件次数，
        不等同于去重互动用户数；如果需要后者，应增加独立字段。
        """

        if (
            window_start.tzinfo is None
            or window_start.utcoffset() is None
        ):
            raise ValueError("window_start must be timezone-aware")

        if (
            window_end.tzinfo is None
            or window_end.utcoffset() is None
        ):
            raise ValueError("window_end must be timezone-aware")

        if window_start >= window_end:
            raise ValueError("window_start must be less than window_end")

        seen_events: dict[str, BehaviorRecord] = {}

        # key: (news_id, content_type)
        groups: dict[tuple[str, ContentType], _MetricAccumulator] = {}

        for record in records:
            record.validate()

            if not (window_start <= record.event_time < window_end):
                continue

            if not source_guarantees_unique_event_id:
                old_record = seen_events.get(record.event_id)

                if old_record is not None:
                    # 完全相同 -> 当作重复事件
                    if old_record == record:
                        continue

                    # 相同ID对应不同的内容
                    raise ValueError(
                        f"event_id {record.event_id} has conflicting records"
                    )

                seen_events[record.event_id] = record

            # 按照 news_id + content_type 分组。
            key = (record.news_id, record.content_type)
            if key not in groups:
                groups[key] = {
                    "impressions": 0,
                    "clicks": 0,
                    "user_ids": set(),
                    "total_duration_seconds": 0,
                    "effective_consumptions": 0,
                    "interactions": 0,
                }

            group = groups[key]
            group["user_ids"].add(record.user_id)

            if record.event_type == EventType.IMPRESSION:
                group["impressions"] += 1
            elif record.event_type == EventType.CLICK:
                group["clicks"] += 1
            elif (
                record.content_type == ContentType.ARTICLE
                and record.event_type == EventType.READ
            ):
                group["total_duration_seconds"] += record.duration_seconds
                if record.duration_seconds >= self.article_effective_seconds:
                    group["effective_consumptions"] += 1
            elif (
                record.content_type == ContentType.VIDEO
                and record.event_type == EventType.PLAY
            ):
                group["total_duration_seconds"] += record.duration_seconds
                if record.duration_seconds >= self.video_effective_seconds:
                    group["effective_consumptions"] += 1

            if record.event_type in {
                EventType.LIKE,
                EventType.COMMENT,
                EventType.SHARE,
                EventType.FAVORITE,
            }:
                group["interactions"] += 1

        # 转成最终 Snapshot 对象
        snapshots: list[NewsMetricSnapshot] = []

        for (news_id, content_type), group in groups.items():
            impressions = group["impressions"]
            clicks = group["clicks"]

            if impressions == 0:
                ctr = Decimal("0")
            else:
                ctr = Decimal(clicks) / Decimal(impressions)

            # 快照
            snapshot = NewsMetricSnapshot(
                news_id=news_id,
                content_type=content_type,
                window_start=window_start,
                window_end=window_end,
                impressions=impressions,
                clicks=clicks,
                unique_users=len(group["user_ids"]),
                total_duration_seconds=group["total_duration_seconds"],
                effective_consumptions=group["effective_consumptions"],
                interactions=group["interactions"],
                ctr=ctr,
            )

            snapshots.append(snapshot)

        # 固定排序，保证结果稳定性
        snapshots.sort(
            key=lambda s: (s.news_id, s.content_type, s.window_start, s.window_end)
        )

        return snapshots
