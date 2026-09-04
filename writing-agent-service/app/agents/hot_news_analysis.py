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

    def _calculate_derived_metrics(
        self,
        input_data: HotNewsAnalysisInput,
    ) -> dict[str, float]:
        """计算运营分析需要的派生指标。"""
        

    def _build_trend_summary(self, input_data: HotNewsAnalysisInput) -> str:
        """TODO：加入热点等级、环比变化及异常波动判断。"""


        return f"《{input_data.title}》当前热点分数为 {input_data.hot_score:.4f}。"
        
    def _build_attention_reasons(
        self,
        input_data: HotNewsAnalysisInput,
    ) -> tuple[str, ...]:
        """TODO：根据各分量贡献识别点击、消费、互动或增长驱动。"""
        components = {
            "点击表现": input_data.metrics.get("click_component", 0.0),
            "有效消费": input_data.metrics.get("consumption_component", 0.0),
            "用户互动": input_data.metrics.get("interaction_component", 0.0),
            "热度增长": input_data.metrics.get("growth_component", 0.0),
        }
        reason, value = max(components.items(), key=lambda item: item[1])
        if value <= 0:
            return ("当前指标不足以判断主要关注原因",)
        return (f"{reason}是当前热度的主要贡献项",)

    def _build_related_context(
        self,
        input_data: HotNewsAnalysisInput,
    ) -> tuple[str, ...]:
        """TODO：将关联报道组织成事件背景或时间线，而不只是标题列表。"""
        return tuple(
            f"{item.news.title}（关联分数 {item.final_score:.4f}）"
            for item in input_data.related_news
        )

    def _build_operation_suggestions(
        self,
        input_data: HotNewsAnalysisInput,
    ) -> tuple[str, ...]:
        """TODO：根据业务指标和证据充分度补充可执行运营策略。"""
        if not input_data.related_news:
            return ("知识库证据不足，建议补充检索并人工确认后再形成运营结论",)
        return ("结合高相关报道补充事件背景，并持续观察后续用户行为变化",)
