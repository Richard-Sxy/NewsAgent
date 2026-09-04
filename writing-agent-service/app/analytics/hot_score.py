"""基于指标快照计算可解释的新闻热度。"""

from dataclasses import dataclass
from decimal import Decimal

from app.analytics.metrics import NewsMetricReference, NewsMetricSnapshot

ZERO = Decimal("0")
ONE = Decimal("1")
TOLERANCE = Decimal("0.0001")  # 用于浮点数比较的容差


@dataclass(frozen=True, slots=True)
class HotScoreConfig:
    """第一版线性热度公式的权重，所有权重之和应为 1。"""

    click_weight: Decimal = Decimal("0.30")
    effective_consumption_weight: Decimal = Decimal("0.35")
    interaction_weight: Decimal = Decimal("0.20")
    growth_weight: Decimal = Decimal("0.15")

    def validate(self) -> None:
        """校验每个权重位于 [0, 1] 且总和约等于 1。"""
        weights = {
            "click_weight": self.click_weight,
            "effective_consumption_weight": self.effective_consumption_weight,
            "interaction_weight": self.interaction_weight,
            "growth_weight": self.growth_weight,
        }

        for name, weight in weights.items():
            if not (ZERO <= weight <= ONE):
                raise ValueError(f"{name} must be between 0 and 1, got {weight}")

        total = sum(weights.values(), ZERO)            # 这个是初始值的含义，后面再添加元素
        if not (abs(total - ONE) < TOLERANCE):
            raise ValueError(f"Weight sum must be 1, got {total}")


@dataclass(frozen=True, slots=True)
class HotScoreResult:
    news_id: str
    score: Decimal
    click_component: Decimal
    consumption_component: Decimal
    interaction_component: Decimal
    growth_component: Decimal


class HotScoreCalculator:
    """计算热度分数；不查询数据库，也不调用大模型。"""

    def __init__(self, config: HotScoreConfig | None = None) -> None:
        self.config = config or HotScoreConfig()

    def calculate(
        self,
        current: NewsMetricSnapshot,
        baseline: NewsMetricReference | None,
    ) -> HotScoreResult:
        """根据当前窗口和历史基线计算热度及各分量。

        点击、有效消费和互动均转换为 [0, 1] 比例；增长率经过压缩后再加权。
        第一版不直接比较图文阅读时长与视频播放时长，而使用已经按内容类型阈值
        标准化的有效消费次数。
        """

        # 校验配置
        self.config.validate()
        # baseline 如果存在，应该是与 current 相同的 news_id 和 content_type
        if baseline is not None:
            if baseline.news_id != current.news_id:
                raise ValueError(
                    f"Baseline news_id {baseline.news_id} does not match current {current.news_id}"
                )
            if baseline.content_type != current.content_type:
                raise ValueError(
                    f"Baseline content_type {baseline.content_type} does not match current {current.content_type}"
                )
        # 点击质量
        click_rate = self._safe_ratio(
            current.clicks,
            current.impressions,
        )

        # 3. 有效消费质量：
        # 点击之后，有多少真正形成了有效阅读/播放
        consumption_rate = self._safe_ratio(
            current.effective_consumptions,
            current.clicks,
        )

        # 4. 互动质量：
        # 有效消费后产生多少点赞/评论/分享/收藏
        interaction_rate = self._safe_ratio(
            current.interactions,
            current.effective_consumptions,
        )

        # 5. 增长率
        growth_rate = self._calculate_growth(
            current,
            baseline,
        )

        # 分别乘权重
        click_component = (
            click_rate
            * self.config.click_weight
        )

        consumption_component = (
            consumption_rate
            * self.config.effective_consumption_weight
        )

        interaction_component = (
            interaction_rate
            * self.config.interaction_weight
        )

        growth_component = (
            growth_rate
            * self.config.growth_weight
        )

        # 6. 最终热度
        score = (
            click_component
            + consumption_component
            + interaction_component
            + growth_component
        )

        return HotScoreResult(
            news_id=current.news_id,
            score=score,
            click_component=click_component,
            consumption_component=consumption_component,
            interaction_component=interaction_component,
            growth_component=growth_component,
        )
    
    @staticmethod
    def _safe_ratio(
        numerator: int,
        denominator: int,
    ) -> Decimal:
        """安全计算比例，并限制到 [0, 1]。"""

        if denominator <= 0:
            return ZERO

        ratio = (
            Decimal(numerator)
            / Decimal(denominator)
        )

        if ratio < ZERO:
            return ZERO

        if ratio > ONE:
            return ONE

        return ratio

    def _calculate_growth(
        self,
        current: NewsMetricSnapshot,
        baseline: NewsMetricReference | None,
    ) -> Decimal:
        """计算当前窗口相对历史窗口的增长分数。"""

        # 没有历史基线时，不凭空认为它增长
        if baseline is None:
            return ZERO

        current_activity = (
            current.clicks
            + current.effective_consumptions
            + current.interactions
        )

        baseline_activity = (
            baseline.clicks
            + baseline.effective_consumptions
            + baseline.interactions
        )

        # 历史为 0 时无法计算合理增长率
        if baseline_activity <= 0:
            return ZERO

        growth = (
            Decimal(current_activity - baseline_activity)
            / Decimal(baseline_activity)
        )

        # 第一版只奖励增长，不因为下降产生负热度
        if growth <= ZERO:
            return ZERO

        # 将可能无限大的增长率压缩到 [0, 1)
        #
        # 100%增长：
        # 1 / (1 + 1) = 0.5
        #
        # 200%增长：
        # 2 / (1 + 2) ≈ 0.667
        return growth / (ONE + growth)
