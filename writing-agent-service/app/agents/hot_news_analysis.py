"""热点分析 Agent 的结构化输入、输出与第一版规则框架。"""

from dataclasses import dataclass

from app.analytics.hot_news_enrichment import EnrichedHotNews
from app.retrieval.related_news_reranker import RerankRelatedNews


@dataclass(frozen=True, slots=True)
class HotNewsAnalysisInput:
    news_id: str
    title: str
    hot_score: float
    metrics: dict[str, float]
    related_news: tuple[RerankRelatedNews, ...]


@dataclass(frozen=True, slots=True)
class HotNewsAnalysisResult:
    news_id: str
    trend_summary: str
    attention_reasons: tuple[str, ...]
    related_context: tuple[str, ...]
    operation_suggestions: tuple[str, ...]
    evidence_news_ids: tuple[str, ...]


def build_analysis_input(item: EnrichedHotNews) -> HotNewsAnalysisInput:
    """把排行榜富化结果转换成 Agent 的稳定输入契约。"""
    if item.content is None:
        raise ValueError("cannot analyze hot news without content")

    current = item.ranking.current
    score = item.ranking.hot_score
    # 新闻热点分析结果对象
    return HotNewsAnalysisInput(
        news_id=current.news_id,
        title=item.content.title,
        hot_score=float(score.score),
        metrics={
            "impressions": float(current.impressions),
            "clicks": float(current.clicks),
            "ctr": float(current.ctr),
            "effective_consumptions": float(current.effective_consumptions),
            "interactions": float(current.interactions),
            "click_component": float(score.click_component),
            "consumption_component": float(score.consumption_component),
            "interaction_component": float(score.interaction_component),
            "growth_component": float(score.growth_component),
        },
        related_news=item.related_news,
    )

"""不调用大模型的第一版分析器；后续 LLM 只负责解释和表达。"""
class RuleBasedHotNewsAnalyzer:

    def analyze(self, input_data: HotNewsAnalysisInput) -> HotNewsAnalysisResult:
        if not input_data.news_id.strip():
            raise ValueError("news_id cannot be empty")
        if not input_data.title.strip():
            raise ValueError("title cannot be empty")

        return HotNewsAnalysisResult(
            news_id=input_data.news_id,
            trend_summary=self._build_trend_summary(input_data),
            attention_reasons=self._build_attention_reasons(input_data),
            related_context=self._build_related_context(input_data),
            operation_suggestions=self._build_operation_suggestions(input_data),
            evidence_news_ids=tuple(
                item.news.news_id
                for item in input_data.related_news
                if item.news.news_id
            ),
        )

    """计算派生指标"""
    def _calculate_derived_metrics(
        self,
        input_data: HotNewsAnalysisInput,
    ) -> dict[str, float]:
        """计算运营分析需要的派生指标。"""
        metrics = input_data.metrics

        impressions = metrics.get("impressions", 0.0)
        clicks = metrics.get("clicks", 0.0)
        effective_consumptions = metrics.get("effective_consumptions", 0.0)
        interactions = metrics.get("interactions", 0.0)

        # 点击率
        click_rate = (clicks/impressions if impressions > 0 else 0.0)
        # 点击以后真正形成的有效消费比例
        effective_consumptions_rate = (
            effective_consumptions / impressions
            if impressions > 0
            else 0.0
        )
        # 点击用户产生互动的比例
        interaction_rate = (
            interactions / clicks
            if clicks > 0
            else 0.0
        )
        # 四个热点分量。
        components = {
            "click": metrics.get("click_component", 0.0),
            "consumption": metrics.get(
                "consumption_component",
                0.0,
            ),
            "interaction": metrics.get(
                "interaction_component",
                0.0,
            ),
            "growth": metrics.get("growth_component", 0.0),
        }

        total_component = sum(
            max(value, 0.0)
            for value in components.values()
        )

        return {
            "click_rate": click_rate,
            "effective_consumption_rate":
                effective_consumptions_rate,
            "interaction_rate": interaction_rate,
            "click_share": self._safe_share(
                components["click"],
                total_component,
            ),
            "consumption_share": self._safe_share(
                components["consumption"],
                total_component,
            ),
            "interaction_share": self._safe_share(
                components["interaction"],
                total_component,
            ),
            "growth_share": self._safe_share(
                components["growth"],
                total_component,
            ),
        }

    def _build_trend_summary(self, input_data: HotNewsAnalysisInput) -> str:
        """TODO：加入热点等级、环比变化及异常波动判断。"""
        hot_level = self._get_hot_level(
            input_data.hot_score
        )

        metrics = input_data.metrics
        derived = self._calculate_derived_metrics(
            input_data
        )

        impressions = int(metrics.get("impressions", 0))
        clicks = int(metrics.get("clicks", 0))

        return (
            f"《{input_data.title}》当前处于"
            f"{hot_level}状态，热点分数为"
            f"{input_data.hot_score:.4f}"
            f"当前累计曝光 {impressions} 次"
            f"点击 {clicks} 次"
            f"{derived['click_rate']:.2%}。"
        )


    def _build_attention_reasons(
        self,
        input_data: HotNewsAnalysisInput,
    ) -> tuple[str, ...]:
        """TODO：根据各分量贡献识别点击、消费、互动或增长驱动。"""
        derived = self._calculate_derived_metrics(input_data)
        components = {
            "点击表现": (
                input_data.metrics.get("click_component", 0.0),
                derived["click_share"],
            ),
            "有效消费": (
                input_data.metrics.get("consumption_component", 0.0),
                derived["consumption_share"],
            ),
            "用户互动": (
                input_data.metrics.get("interaction_component", 0.0),
                derived["interaction_share"],
            ),
            "热度增长": (
                input_data.metrics.get("growth_component", 0.0),
                derived["growth_share"],
            ),
        }
        sorted_components = sorted(
            components.items(),
            key=lambda item: item[1][0],
            reverse=True,
        )
        reasons: list[str] = []
        # 第一驱动因素。
        primary_name, (
            primary_value,
            primary_share,
        ) = sorted_components[0]

        if primary_value <= 0:
            return (
                "当前各项热点指标贡献都低"
                "暂时无法识别明确的热度驱动因素",
            )
        
        reasons.append(
            f"{primary_name}是当前热度的主要贡献项"
            f"约占热点贡献的 {primary_share:.1f}"
        )

        secondary_name, (
            secondary_value,
            secondary_share,
        ) = sorted_components[1]

        if (
            secondary_value > 0
            and secondary_share >= 0.25
        ):
            reasons.append(
                f"{secondary_name}同样表现突出，约占热点贡献的"
                f" {secondary_share:.1%}，当前热点呈现多因素驱动特征。"
            )
        if (
            derived["effective_consumption_rate"] >= 0.60
        ):
            reasons.append( "点击用户中有消费比例较高，说明用户不仅点击，还存在明显的深度阅读行为" )
        if derived["interaction_rate"] >= 0.10:
            reasons.append( "用户互动比例较高，该事件具有进一步讨论和传播潜力" )

        return tuple(reasons)

    def _build_related_context(
        self,
        input_data: HotNewsAnalysisInput,
    ) -> tuple[str, ...]:
        """TODO：将关联报道组织成事件背景或时间线，而不只是标题列表。"""
        if not input_data.related_news:
            return ("当前知识库暂未检索到高相关的历史报道",)

        contexts: list[str] = []

        for index, item in enumerate( input_data.related_news[:5], start=1 ):
            contexts.append(
                f"关联报道 {index}："
                f"《{item.news.title}》"
                f"关联分数 {item.final_score:.4f}"
            )

        return tuple(contexts)

    def _build_operation_suggestions(
        self,
        input_data: HotNewsAnalysisInput,
    ) -> tuple[str, ...]:
        """TODO：根据业务指标和证据充分度补充可执行运营策略。"""
        derived = self._calculate_derived_metrics(input_data)

        suggestions: list[str] = []

        dominant = max(
            {
                "click": derived["click_share"],
                "consumption":
                    derived["consumption_share"],
                "interaction":
                    derived["interaction_share"],
                "growth": derived["growth_share"]
            },
            key=lambda key: {
                "click": derived["click_share"],
                "consumption":
                    derived["consumption_share"],
                "interaction":
                    derived["interaction_share"],
                "growth": derived["growth_share"],
            }[key],
        )

        if dominant == "growth":
            suggestions.append(
                "当前热度主要由增长指标驱动，建议提高监控频率，持续观察事件，是否进入快速扩散阶段"
            )
        elif dominant == "click":
            suggestions.append(
                "当前新闻点击表现突出，建议继续观察有效消费和互动指标，判断热点是否能够从高点击转化为持续关注"
            )
        elif dominant == "consumption":
            suggestions.append(
                "用户深度表现良好，建议补充事件背景、人物关系和时间线内容，增强专题运营"
            )
        elif dominant == "interaction":
            suggestions.append(
                "用户互动表现突出，建议关注评论区核心观点和争议议题、并评估是否形成适合形成后续持续追踪专题"
            )
        if len(input_data.related_news) >= 3:
            suggestions.append(
                "当前已经有充分的关联报道证据，"
                "可以围绕事件发展脉络整理背景资料"
                "或形成专题研究"
            )
        elif input_data.related_news:
            suggestions.append(
                "当前已有少量关联报道，"
                "建议继续补充检索后再形成完整"
                "事件时间线"
            )
        else:
            suggestions.append(
                "知识库证据不足，建议补充检索，"
                "并在人工确认后再形成事件背景结论"
            )

        return tuple(suggestions)

    @staticmethod
    def _safe_share(
        value: float,
        total: float,
    ) -> float:
        if total <= 0:
            return 0.0

        return max(value, 0.0) / total

    @staticmethod
    def _get_hot_level(
        hot_score: float,
    ) -> str:
        """第一版热点等级规则，后续根据线上历史热点分数分布，改成分位数阈值"""
        if hot_score <= 0.2:
            return "低热度"
        if hot_score <= 0.5:
            return "中热度"
        if hot_score <= 0.8:
            return "中高热度"
        return "高热度"
