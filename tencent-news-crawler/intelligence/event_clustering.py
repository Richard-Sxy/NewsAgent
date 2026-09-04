from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from intelligence.extraction.entity_extractor import (
    entity_similarity,
    extract_entities,
)
from intelligence.extraction.numeric_extractor import (
    extract_key_numbers,
    numeric_similarity,
)

from intelligence.schemas import (
    ClusterMember,
    EventArticle,
    EventCluster,
    EventDataset,
)

# 连续匹配英文字符/连续匹配中文字符
TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+._-]*|\d+(?:\.\d+)?%?")
CHINESE_PATTERN = re.compile(r"[\u4e00-\u9fff]+")
STOP_TOKENS = {
    "一个", "一些", "以及", "目前", "表示", "进行", "已经", "这个",
    "记者", "报道", "消息", "新闻", "腾讯", "可以", "没有", "相关",
}


def tokenize(text: str) -> list[str]:
    """轻量分词：英文/数字词元 + 中文连续片段的二元组。"""
    normalized = re.sub(r"\s+", " ", text).lower()
    tokens = [match.group(0) for match in TOKEN_PATTERN.finditer(normalized)]
    for segment in CHINESE_PATTERN.findall(normalized):
        if len(segment) == 1:
            tokens.append(segment)
            continue
        # 中文内容做了二元分组
        tokens.extend(segment[index:index + 2] for index in range(len(segment) - 1))
    return [token for token in tokens if token not in STOP_TOKENS]


class TfidfSpace:
    """无第三方依赖的稀疏 TF-IDF 空间。"""

    def __init__(self, documents: list[str]):
        tokenized = [tokenize(document) for document in documents]
        document_frequency: Counter[str] = Counter()
        for tokens in tokenized:
            document_frequency.update(set(tokens))
        total = max(len(documents), 1)
        self.idf = {
            token: math.log((1 + total) / (1 + frequency)) + 1
            for token, frequency in document_frequency.items()
        }
        self.vectors = [self._vector(tokens) for tokens in tokenized]

    def _vector(self, tokens: list[str]) -> dict[str, float]:
        counts = Counter(tokens)
        weighted = {
            token: (1 + math.log(count)) * self.idf[token]
            for token, count in counts.items()
        }
        norm = math.sqrt(sum(value * value for value in weighted.values()))
        if not norm:
            return {}
        return {token: value / norm for token, value in weighted.items()}

    @staticmethod
    def cosine(left: dict[str, float], right: dict[str, float]) -> float:
        if len(left) > len(right):
            left, right = right, left
        return sum(value * right.get(token, 0.0) for token, value in left.items())


def parse_publish_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

"""计算事件衰减"""
def temporal_similarity(left: EventArticle, right: EventArticle, decay_days: float) -> float:
    left_time = parse_publish_time(left.publish_time)
    right_time = parse_publish_time(right.publish_time)
    if left_time is None or right_time is None:
        return 0.5
    difference = abs((left_time - right_time).total_seconds()) / 86400
    return math.exp(-difference / max(decay_days, 0.01))     # 指数衰减


@dataclass(frozen=True)
class SimilarityWeights:
    title: float = 0.50
    content: float = 0.20
    entity: float = 0.20
    # 简单数字 Jaccard 在首轮消融实验中未提升 F1，默认关闭；
    # 保留该权重用于后续类型化数字冲突实验。
    numeric: float = 0.0
    time: float = 0.10

    def validate(self) -> None:
        values = (
            self.title,
            self.content,
            self.entity,
            self.numeric,
            self.time,
        )
        
        if any(value < 0 for value in values):
            raise ValueError("相似度权重不能为负数。")
        if not math.isclose(sum(values), 1.0, abs_tol=1e-6):
            raise ValueError("相似度权重之和必须为 1。")


class UnionFind:
    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


class EventClusterer:
    """基于 TF-IDF、时间窗口与相似度阈值的事件聚类基线。"""

    def __init__(
        self,
        threshold: float = 0.48,
        max_time_gap_days: float = 3.0,
        time_decay_days: float = 1.5,
        weights: SimilarityWeights | None = None,
    ):
        if not 0 <= threshold <= 1:
            raise ValueError("threshold 必须位于 0 到 1 之间。")
        if max_time_gap_days <= 0:
            raise ValueError("max_time_gap_days 必须大于 0。")
        self.threshold = threshold
        self.max_time_gap_days = max_time_gap_days
        self.time_decay_days = time_decay_days
        self.weights = weights or SimilarityWeights()
        self.weights.validate()

    def cluster(self, articles: list[EventArticle]) -> EventDataset:
        if not articles:
            return EventDataset(
                method="tfidf_entity_numeric_time_v2",
                parameters=self._parameters(),
                article_count=0,
                cluster_count=0,
                multi_article_cluster_count=0,
                clusters=[],
            )

        title_space = TfidfSpace([article.title for article in articles])
        content_space = TfidfSpace([
            f"{article.title} {article.content[:800]}" for article in articles
        ])
        entity_sets = [extract_entities(article.title) for article in articles]
        numeric_sets = [extract_key_numbers(article.title) for article in articles]
        union_find = UnionFind(len(articles))

        for left_index, left in enumerate(articles):
            for right_index in range(left_index + 1, len(articles)):
                right = articles[right_index]
                if not self._within_time_window(left, right):
                    continue
                score = self._score(
                    left,
                    right,
                    title_space.vectors[left_index],
                    title_space.vectors[right_index],
                    content_space.vectors[left_index],
                    content_space.vectors[right_index],
                    entity_sets[left_index],
                    entity_sets[right_index],
                    numeric_sets[left_index],
                    numeric_sets[right_index],
                )
                if score >= self.threshold:
                    union_find.union(left_index, right_index)

        groups: dict[int, list[int]] = defaultdict(list)
        for index in range(len(articles)):
            groups[union_find.find(index)].append(index)

        clusters = [
            self._build_cluster(
                indexes,
                articles,
                title_space,
                content_space,
                entity_sets,
                numeric_sets,
            )
            for indexes in groups.values()
        ]
        clusters.sort(key=lambda item: (item.start_time, item.event_id), reverse=True)
        return EventDataset(
            method="tfidf_entity_numeric_time_v2",
            parameters=self._parameters(),
            article_count=len(articles),
            cluster_count=len(clusters),
            multi_article_cluster_count=sum(cluster.article_count > 1 for cluster in clusters),
            clusters=clusters,
        )

    def _parameters(self) -> dict[str, float | int | str]:
        return {
            "threshold": self.threshold,
            "max_time_gap_days": self.max_time_gap_days,
            "time_decay_days": self.time_decay_days,
            "title_weight": self.weights.title,
            "content_weight": self.weights.content,
            "entity_weight": self.weights.entity,
            "numeric_weight": self.weights.numeric,
            "time_weight": self.weights.time,
        }

    # 三天的新闻长度窗口
    def _within_time_window(self, left: EventArticle, right: EventArticle) -> bool:
        left_time = parse_publish_time(left.publish_time)
        right_time = parse_publish_time(right.publish_time)
        if left_time is None or right_time is None:
            return True
        difference = abs((left_time - right_time).total_seconds()) / 86400
        return difference <= self.max_time_gap_days

    def _score(
        self,
        left: EventArticle,
        right: EventArticle,
        left_title: dict[str, float],
        right_title: dict[str, float],
        left_content: dict[str, float],
        right_content: dict[str, float],
        left_entities: set[str],
        right_entities: set[str],
        left_numbers: set[str],
        right_numbers: set[str],
    ) -> float:
        entity_score = entity_similarity(
            left_entities,
            right_entities,
        )
        numeric_score = numeric_similarity(
            left_numbers,
            right_numbers,
        )

        return (
            self.weights.title * TfidfSpace.cosine(left_title, right_title)
            + self.weights.content * TfidfSpace.cosine(left_content, right_content)
            + self.weights.entity * entity_score
            + self.weights.numeric * numeric_score
            + self.weights.time * temporal_similarity(left, right, self.time_decay_days)
        )

    def _build_cluster(
        self,
        indexes: list[int],
        articles: list[EventArticle],
        title_space: TfidfSpace,
        content_space: TfidfSpace,
        entity_sets: list[set[str]],
        numeric_sets: list[set[str]],
    ) -> EventCluster:
        representative_index = self._representative(indexes, title_space)
        representative = articles[representative_index]
        ordered = sorted(indexes, key=lambda index: articles[index].publish_time)
        members = []
        for index in ordered:
            article = articles[index]
            similarity = self._score(
                representative,
                article,
                title_space.vectors[representative_index],
                title_space.vectors[index],
                content_space.vectors[representative_index],
                content_space.vectors[index],
                entity_sets[representative_index],
                entity_sets[index],
                numeric_sets[representative_index],
                numeric_sets[index],
            )
            members.append(ClusterMember(
                article_id=article.article_id,
                url=article.url,
                title=article.title,
                publish_time=article.publish_time,
                topic=article.topic,
                similarity_to_representative=round(similarity, 4),
            ))
        identity = "|".join(sorted(article.article_id for article in (articles[i] for i in indexes)))
        event_id = f"event-{sha256(identity.encode()).hexdigest()[:12]}"
        keywords = self._keywords(indexes, title_space)
        return EventCluster(
            event_id=event_id,
            representative_article_id=representative.article_id,
            event_title=representative.title,
            start_time=articles[ordered[0]].publish_time,
            end_time=articles[ordered[-1]].publish_time,
            article_count=len(indexes),
            keywords=keywords,
            needs_review=len(indexes) > 1,
            members=members,
        )

    @staticmethod
    def _representative(indexes: list[int], space: TfidfSpace) -> int:
        if len(indexes) == 1:
            return indexes[0]
        return max(
            indexes,
            key=lambda candidate: sum(
                TfidfSpace.cosine(space.vectors[candidate], space.vectors[other])
                for other in indexes
                if other != candidate
            ),
        )

    @staticmethod
    def _keywords(indexes: list[int], space: TfidfSpace, limit: int = 10) -> list[str]:
        scores: Counter[str] = Counter()
        for index in indexes:
            scores.update(space.vectors[index])
        return [token for token, _ in scores.most_common(limit)]
