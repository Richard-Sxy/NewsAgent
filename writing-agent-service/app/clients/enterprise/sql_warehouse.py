"""只读 SQL 数仓执行端口与本地替身。

该端口只负责执行已经通过 ``SqlGuard`` 的 SQL，并返回规范化行；它不生成 SQL，
也不做业务校验。真实实现应由企业 SDK/RPC 提供，并使用只读账号和连接级超时。
"""

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from app.domain.errors import HotNewsDataQualityError

SqlRow = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SqlQuery:
    """一次只读查询；``params`` 使用命名占位符绑定，禁止字符串拼接。"""

    sql: str
    params: Mapping[str, Any]
    timeout_ms: int
    max_rows: int


@dataclass(frozen=True, slots=True)
class SqlQueryResult:
    rows: tuple[SqlRow, ...]
    elapsed_ms: int
    truncated: bool = False


class SqlWarehouseClient(Protocol):
    async def execute(self, query: SqlQuery) -> SqlQueryResult:
        """执行只读查询并返回行；失败应抛领域错误而不是返回空结果。"""
        ...


class InMemorySqlWarehouseClient:
    """本地学习和测试替身，不连接或复制企业数仓。

    ``handler`` 可用于按查询动态返回不同行；未提供时固定返回构造时的行。
    """

    def __init__(
        self,
        rows: tuple[SqlRow, ...] = (),
        *,
        elapsed_ms: int = 1,
        handler: Callable[[SqlQuery], tuple[SqlRow, ...]] | None = None,
    ) -> None:
        self._rows = tuple(rows)
        self._elapsed_ms = elapsed_ms
        self._handler = handler
        self.queries: list[SqlQuery] = []

    async def execute(self, query: SqlQuery) -> SqlQueryResult:
        self.queries.append(query)
        rows = (
            tuple(self._handler(query))
            if self._handler is not None
            else self._rows
        )
        if query.max_rows <= 0:
            raise HotNewsDataQualityError("sql query max_rows must be positive")
        truncated = len(rows) > query.max_rows
        if truncated:
            rows = rows[: query.max_rows]
        return SqlQueryResult(
            rows=rows,
            elapsed_ms=self._elapsed_ms,
            truncated=truncated,
        )
