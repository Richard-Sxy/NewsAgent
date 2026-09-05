"""企业行为数据库与领域计算之间的防腐层。"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.analytics.entities import BehaviorRecord, ContentType


@dataclass(frozen=True, slots=True)
class BehaviorQuery:
    """行为数据查询条件；使用左闭右开区间 [start, end)。

    ``tenant_id`` 由编排层传入，真实企业数据适配器必须据此执行租户隔离；
    内存实现只用于不含真实用户数据的本地测试。
    """

    start: datetime
    end: datetime
    tenant_id: str | None = None
    news_ids: frozenset[str] = field(default_factory=frozenset)
    content_types: frozenset[ContentType] = field(default_factory=frozenset)

    def validate(self) -> None:
        """检查时间带时区、start < end，以及 news_ids 中无空值。"""
        if (
            self.start.tzinfo is None
            or self.start.utcoffset() is None
        ):
            raise ValueError("start must be timezone-aware")
        if (
            self.end.tzinfo is None
            or self.end.utcoffset() is None
        ):
            raise ValueError("end must be timezone-aware")
        if (self.start >= self.end):
            raise ValueError("start must be less than end")
        if self.tenant_id is not None and not self.tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")

        for news_id in self.news_ids:
            if not news_id.strip():
                raise ValueError("news_ids cannot contain empty strings")
        


class BehaviorDataSource(Protocol):
    """企业数据库适配器必须满足的最小接口。

    真实实现可以通过企业 SQL 客户端或内部 API 获取数据；计算层只依赖此协议，
    不关心底层数据库类型。实现类应强制执行 ``query.tenant_id`` 对应的权限隔离，
    并且只查询获得授权且分析必需的字段。
    """

    def fetch(self, query: BehaviorQuery) -> list[BehaviorRecord]:
        """返回已经完成字段名、枚举值和时间格式映射的领域对象。"""

        ...


class InMemoryBehaviorDataSource:
    """本地学习使用的假数据源，不连接或复制企业数据库。"""

    def __init__(self, records: list[BehaviorRecord]) -> None:
        self._records = list(records)

    def fetch(self, query: BehaviorQuery) -> list[BehaviorRecord]:
        """按查询条件过滤内存记录，返回符合条件的 BehaviorRecord 列表。"""

        query.validate()

        result: list[BehaviorRecord] = []

        for record in self._records:
            record.validate()
            # 使用[start, end)过滤时间
            if not (query.start <= record.event_time < query.end):
                continue

            # news_ids非空才过滤
            if query.news_ids and record.news_id not in query.news_ids:
                continue

            # content_types非空才过滤
            if query.content_types and record.content_type not in query.content_types:
                continue

            result.append(record)

        return result
