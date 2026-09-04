"""根据当前指标和历史基线生成热点新闻排行榜。"""

from dataclasses import dataclass

from app.analytics.baseline import NewsMetricBaseline
from app.analytics.entities import ContentType
from app.analytics.hot_score import HotScoreCalculator, HotScoreResult
from app.analytics.metrics import NewsMetricSnapshot


MetricKey = tuple[str, ContentType]


@dataclass(frozen=True, slots=True)
class RankedHotNews:
    """排行榜中的一项，保留当前指标、基线和可解释分数。"""

    rank: int
    current: NewsMetricSnapshot
    baseline: NewsMetricBaseline | None
    hot_score: HotScoreResult


class HotNewsRanker:
    def __init__(self, score_calculator: HotScoreCalculator | None = None) -> None:
        self.score_calculator = score_calculator or HotScoreCalculator()

    def rank(
        self,
        current_snapshots: list[NewsMetricSnapshot],
        baselines: dict[MetricKey, NewsMetricBaseline],
        *,
        limit: int = 20,
    ) -> list[RankedHotNews]:
        """计算所有当前新闻的热度并返回 Top N。

        注意：这里排的是单个时间窗口的热点榜。窗口选择和企业数据库查询属于
        上一层服务职责，排行榜不应直接访问数据库。
        """
        if limit <= 0:
            raise ValueError("limit must be greater than 0")

        if not current_snapshots:
            return []

        scored_items: list[
            tuple[NewsMetricSnapshot, NewsMetricBaseline | None, HotScoreResult]
        ] = []

        seen_keys: set[MetricKey] = set()

        for current in current_snapshots:
            key: MetricKey = (current.news_id, current.content_type)
            if key in seen_keys:
                raise ValueError(
                    "current_snapshots 存在重复键: "
                    f"news_id={current.news_id!r}, "
                    f"content_type={current.content_type!r}"
                )
            seen_keys.add(key)

            item_baseline = baselines.get(key)
            score = self.score_calculator.calculate(current, item_baseline)
            scored_items.append((current, item_baseline, score))

        scored_items.sort(
            key=lambda item: (
                -item[2].score,
                item[0].news_id,
                item[0].content_type.value,
            )
        )

        return [
            RankedHotNews(
                rank=rank,
                current=current,
                baseline=item_baseline,
                hot_score=score,
            )
            for rank, (current, item_baseline, score) in enumerate(
                scored_items[:limit],
                start=1,
            )
        ]

            
