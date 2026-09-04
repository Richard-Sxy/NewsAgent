"""对知识库召回的关联新闻进行规则重拍和过滤。"""
from dataclasses import dataclass
from datetime import datetime

from app.analytics.news_content import NewsContent
from app.clients.knowledge_base import RelatedNews
from app.retrieval.news_features import NewsFeatures, extract_news_features

@dataclass(frozen=True, slots=True)
class RerankRelatedNews:
    news: RelatedNews
    final_score: float
    vector_score: float
    entity_score: float
    event_score: float
    keyword_score: float
    time_score: float
    reasons: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class RerankerConfig:
    vector_weight: float = 0.40
    entity_weight: float = 0.25
    event_weight: float = 0.15
    keyword_weight: float = 0.15
    time_weight: float = 0.05
    # 当前规则词表覆盖有限；0.30 可保留高向量相似候选，再由主体冲突规则降权。
    minimum_score: float = 0.30

class RelatedNewsReranker:
    def __init__(self, config: RerankerConfig | None = None) -> None:
        self.config = config or RerankerConfig()

    def rerank(
        self,
        source: NewsContent,
        candidates: list[RelatedNews],
        *,
        limit: int = 3,
    ) -> list[RerankRelatedNews]:
        source_features = extract_news_features(
            source.title,
            f"{source.summary} {source.body}",
        )

        results: list[RerankRelatedNews] = []

        for candidate in candidates:
            candidate_features = extract_news_features(
                candidate.title,
                candidate.text,
            )

            result = self._score_candidate(
                source,
                source_features,
                candidate,
                candidate_features,
            )

            if result.final_score >= self.config.minimum_score:
                results.append(result)

        results.sort(key=lambda item: item.final_score, reverse=True)
        return results[:limit]

    def _score_candidate(
        self,
        source: NewsContent,
        source_features: NewsFeatures,
        candidate: RelatedNews,
        candidate_features: NewsFeatures,
    ) -> RerankRelatedNews:
        vector_score = candidate.score or 0.0

        entity_score = set_similarity(
            source_features.entities,
            candidate_features.entities,
        )
        keyword_score = set_similarity(
            source_features.keywords,
            candidate_features.keywords,
        )
        event_score = event_similarity(
            source_features.event_type,
            candidate_features.event_type,
        )
        time_score = calculate_time_score(
            source.publish_time,
            candidate.publish_time,
        )
        final_score = (
            vector_score * self.config.vector_weight
            + entity_score * self.config.entity_weight
            + event_score * self.config.event_weight
            + keyword_score * self.config.keyword_weight
            + time_score * self.config.time_weight
        )

        reasons: list[str] = []

        if entity_score > 0:
            reasons.append("关键主体一致")
        if event_score > 0:
            reasons.append("事件类型一致")
        if keyword_score >= 0.3:
            reasons.append("标题关键词相近")
        if time_score >= 0.8:
            reasons.append("发布时间接近")

        if (source_features.entities and candidate_features.entities and not source_features.entities.intersection(candidate_features.entities)):
            final_score *= 0.4
            reasons.append("关键主体不一致，执行降权")

        # 核心主体以标题为准。候选正文可能偶然提及源新闻主体，不能因此
        # 绕过主体约束；例如华为财报不应关联到正文含财务词的黄金新闻。
        source_title_entities = extract_news_features(source.title).entities
        candidate_title_entities = extract_news_features(candidate.title).entities
        if (
            source_title_entities
            and not source_title_entities.intersection(candidate_title_entities)
        ):
            final_score *= 0.4
            reasons.append("候选标题缺少相同核心主体，执行降权")

        return RerankRelatedNews(
            news=candidate,
            final_score=round(final_score, 4),
            vector_score=round(vector_score, 4),
            entity_score=round(entity_score, 4),
            event_score=round(event_score, 4),
            keyword_score=round(keyword_score, 4),
            time_score=round(time_score, 4),
            reasons=tuple(reasons),
        )
        

def set_similarity(
    left: frozenset[str],
    right: frozenset[str],
) -> float:
    """计算两个特征集合的Jaccard相似度。"""
    if not left or not right:
        return 0.0

    intersection = left & right
    union = left | right

    return len(intersection) / len(union)

"""计算事件相似性"""
def event_similarity(
    source_event: str | None,
    candidate_event: str | None,
) -> float:
    if source_event is None or candidate_event is None:
        return 0.0

    return 1.0 if source_event == candidate_event else 0.0

"""计算时间评分"""
def calculate_time_score(
    source_time: datetime,
    candidate_time: str | None,
) -> float:
    if not candidate_time:
        return 0.0

    try:
        parsed_candidate_time = datetime.fromisoformat(candidate_time)
    except ValueError:
        return 0.0

    # 防止一个带时区、一个不带时区时相减报错。
    if source_time.tzinfo and parsed_candidate_time.tzinfo is None:
        parsed_candidate_time = parsed_candidate_time.replace(
            tzinfo=source_time.tzinfo
        )

    difference_days = abs(
        (source_time - parsed_candidate_time).total_seconds()
    ) / 86400

    if difference_days <= 1:
        return 1.0
    if difference_days <= 3:
        return 0.8
    if difference_days <= 7:
        return 0.5
    if difference_days <= 30:
        return 0.2

    return 0.0
