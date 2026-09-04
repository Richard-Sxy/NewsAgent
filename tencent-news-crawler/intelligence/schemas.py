from __future__ import annotations

from pydantic import BaseModel, Field

"""这边是把新闻文本对象转成了统一的数据结构"""

class EventArticle(BaseModel):
    """事件聚类使用的最小文章表示。"""

    article_id: str
    url: str
    title: str
    content: str = Field(repr=False)
    publish_time: str = ""
    author: str = ""
    topic: str = ""

"""聚类成员"""
class ClusterMember(BaseModel):
    article_id: str
    url: str
    title: str
    publish_time: str = ""
    topic: str = ""
    similarity_to_representative: float

"""事件集群"""
class EventCluster(BaseModel):
    event_id: str
    representative_article_id: str
    event_title: str
    start_time: str = ""
    end_time: str = ""
    article_count: int
    keywords: list[str]
    needs_review: bool
    members: list[ClusterMember]

"""事件数据集"""
class EventDataset(BaseModel):
    schema_version: str = "1.0"
    method: str
    parameters: dict[str, float | int | str]
    article_count: int
    cluster_count: int
    multi_article_cluster_count: int
    clusters: list[EventCluster]


class EventPairAnnotation(BaseModel):
    """人工事件关系标注；same_event 用于聚类基线评测。"""

    left_article_id: str
    right_article_id: str
    relation: str = Field(pattern="^(same_event|related_event|unrelated)$")
    note: str = ""
