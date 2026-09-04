"""由多个历史窗口生成单篇新闻的指标基线。"""

from dataclasses import dataclass
from decimal import Decimal

from app.analytics.entities import ContentType
from app.analytics.metrics import NewsMetricSnapshot

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class NewsMetricBaseline:
    """同一篇新闻、同一内容类型在多个历史窗口上的平均指标。

    字段名与热点计算所需的指标接口一致，但数值表示历史窗口均值，不是某个
    真实窗口的计数。第一版使用算术平均，后续可以扩展中位数或同星期基线。
    """

    news_id: str
    content_type: ContentType
    sample_count: int
    impressions: Decimal
    clicks: Decimal
    unique_users: Decimal
    total_duration_seconds: Decimal
    effective_consumptions: Decimal
    interactions: Decimal
    ctr: Decimal


class BaselineCalculator:
    """把历史指标快照归并为可解释的算术平均基线。"""

    def calculate(
        self,
        historical_snapshots: list[NewsMetricSnapshot],
    ) -> NewsMetricBaseline:
        """计算一篇新闻的历史基线。

        第一版不负责选择“过去七天同时段”等样本；调用方应先筛选出具有可比性
        的历史窗口，再交给本方法计算。
        """

        if not historical_snapshots:
            raise ValueError("historical_snapshots cannot be empty")

        first = historical_snapshots[0]

        expected_news_id = first.news_id
        expected_content_type = first.content_type

        # 用于累加
        total_impressions = ZERO
        total_clicks = ZERO
        total_unique_users = ZERO
        total_duration_seconds = ZERO
        total_effective_consumptions = ZERO
        total_interactions = ZERO
        total_ctr = ZERO

        for snapshot in historical_snapshots:
            # 必须属于同一篇新闻
            if snapshot.news_id != expected_news_id:
                raise ValueError("historical_snapshots must have the same news_id")
            if snapshot.content_type != expected_content_type:
                raise ValueError("historical_snapshots must have the same content_type")

            # 校验事件窗口带时区
            if (
                snapshot.window_start.tzinfo is None
                or snapshot.window_start.utcoffset() is None
            ):
                raise ValueError("window_start must be valid datetimes with timezone info")
            if (
                snapshot.window_end.tzinfo is None
                or snapshot.window_end.utcoffset() is None
            ):
                raise ValueError("window_end must be valid datetimes with timezone info")

            # start 必须早于 end
            if snapshot.window_start >= snapshot.window_end:
                raise ValueError("window_start 必须早于 window_end")

            metrics = {
                "impressions": snapshot.impressions,
                "clicks": snapshot.clicks,
                "unique_users": snapshot.unique_users,
                "total_duration_seconds": snapshot.total_duration_seconds,
                "effective_consumptions": snapshot.effective_consumptions,
                "interactions": snapshot.interactions,
                "ctr": snapshot.ctr,
            }
            for name, value in metrics.items():
                if value < 0:
                    raise ValueError(f"{name} 不能为负数")

            # 5. 全部转成 Decimal 再累加
            total_impressions += Decimal(snapshot.impressions)

            total_clicks += Decimal(snapshot.clicks)

            total_unique_users += Decimal(snapshot.unique_users)

            total_duration_seconds += Decimal(snapshot.total_duration_seconds)

            total_effective_consumptions += Decimal(snapshot.effective_consumptions)

            total_interactions += Decimal(snapshot.interactions)

            total_ctr += Decimal(snapshot.ctr)

        sample_count = len(historical_snapshots)
        divisor = Decimal(sample_count)

        return NewsMetricBaseline(
            news_id=expected_news_id,
            content_type=expected_content_type,
            sample_count=sample_count,
            impressions=total_impressions / divisor,
            clicks=total_clicks / divisor,
            unique_users=total_unique_users / divisor,
            total_duration_seconds=total_duration_seconds / divisor,
            effective_consumptions=total_effective_consumptions / divisor,
            interactions=total_interactions / divisor,
            ctr=total_ctr / divisor,
        )
