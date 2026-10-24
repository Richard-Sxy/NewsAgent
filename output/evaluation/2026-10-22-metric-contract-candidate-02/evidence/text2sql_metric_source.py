"""按批准的单桶数仓口径取数，再转换成热点指标快照。

固定模板优先。模型候选 SQL 必须有服务端提供的 ``metric_contract``，且与批准
查询保持相同公式和范围；不能通过模型兜底创造新指标。两条路径均经过通用 SQL
护栏和口径比对。当前每新闻/内容类型只消费一个完整时间桶；跨桶 UV 需要另立合同。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Any, Literal, Mapping

from app.observability.decorators import traced
from app.analytics.entities import ContentType
from app.modules.hot_news.scoring.metrics import (
    BASE_METRIC_KEYS,
    NewsMetricSnapshot,
)
from app.modules.hot_news.scoring.metric_source import HotNewsMetricQuery
from app.modules.hot_news.scoring.metric_sql_contract import validate_metric_sql_contract
from app.analytics.sql_guard import SqlGuard, SqlGuardPolicy
from app.clients.enterprise.sql_warehouse import SqlQuery, SqlWarehouseClient
from app.domain.errors import HotNewsDataQualityError
from app.schemas.text2sql import Text2SqlGenerationInput, Text2SqlSchema
from app.modules.hot_news.analysis.text2sql_agent import Text2SqlAgentRunner


class TemplateNotApplicableError(HotNewsDataQualityError):
    """批准口径没有覆盖请求，不允许由模型临时补造。"""

    retryable = False

@dataclass(frozen=True, slots=True)
class HotNewsMetricSqlTemplate:
    """服务端批准的单桶指标口径，也是候选 SQL 的执行契约。

    ``metric_expressions`` 的键是规范指标名，值是聚合 SQL 表达式。表达式必须
    来自受控配置，不能由模型生成。默认桶为 UTC 对齐的完整小时；其它粒度必须
    显式指定 ``bucket_seconds``，并由上游保证每个输出维度只对应该桶的数据。
    ``group_by_dimensions=False`` 用于上游已按新闻/内容类型/桶聚合的唯一行。
    """

    table: str
    metric_expressions: Mapping[str, str]
    news_id_column: str = "news_id"
    content_type_column: str = "content_type"
    tenant_column: str = "tenant_id"
    window_column: str = "event_time"
    version: str = "hot-news-metric-template-v2"
    bucket_seconds: int = 3600
    group_by_dimensions: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("metric contract version must be nonempty")
        if type(self.bucket_seconds) is not int or not 1 <= self.bucket_seconds <= 604800:
            raise ValueError("metric contract bucket_seconds must be within 1..604800")
        if type(self.group_by_dimensions) is not bool:
            raise ValueError("group_by_dimensions must be boolean")
        expressions = dict(self.metric_expressions)
        if not expressions or any(
            not isinstance(key, str) or not key or not isinstance(value, str) or not value.strip()
            for key, value in expressions.items()
        ):
            raise ValueError("metric contract expressions must be nonempty strings")
        object.__setattr__(self, "metric_expressions", MappingProxyType(expressions))

    @property
    def sha256(self) -> str:
        payload = {
            "version": self.version, "table": self.table,
            "metric_expressions": dict(self.metric_expressions),
            "news_id_column": self.news_id_column, "content_type_column": self.content_type_column,
            "tenant_column": self.tenant_column, "window_column": self.window_column,
            "bucket_seconds": self.bucket_seconds, "group_by_dimensions": self.group_by_dimensions,
        }
        return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def validate_window(self, query: HotNewsMetricQuery) -> None:
        # Same-ZoneInfo subtraction uses wall time across DST transitions.
        # Buckets represent elapsed time, so compare and align in UTC.
        start = query.window_start.astimezone(timezone.utc)
        end = query.window_end.astimezone(timezone.utc)
        if (
            end - start != timedelta(seconds=self.bucket_seconds)
            or start.microsecond
            or int(start.timestamp()) % self.bucket_seconds
        ):
            raise HotNewsDataQualityError(
                "metric contract requires one aligned complete time bucket; "
                "cross-bucket unique users need a separate approved source"
            )

    @property
    def supported_metrics(self) -> frozenset[str]:
        return frozenset(self.metric_expressions)

    @property
    def placeholders(self) -> frozenset[str]:
        return frozenset({"content_type"})

    def render(
        self,
        query: HotNewsMetricQuery,
        *,
        max_rows: int,
    ) -> tuple[str, dict[str, Any]]:
        self.validate_window(query)
        unsupported = query.requested_metrics - (BASE_METRIC_KEYS | {"ctr"})
        if unsupported:
            raise HotNewsDataQualityError(f"unsupported snapshot metrics: {sorted(unsupported)}")
        # CTR is computed by Python from these base counts, not by model SQL.
        metrics = BASE_METRIC_KEYS
        missing = metrics - self.supported_metrics
        if missing:
            raise TemplateNotApplicableError(
                f"template {self.version!r} cannot produce metrics: {sorted(missing)}"
            )

        projections = [
            f"{self.news_id_column} AS news_id",
            f"{self.content_type_column} AS content_type",
        ]
        projections.extend(
            f"{self.metric_expressions[metric]} AS {metric}"
            for metric in sorted(metrics)
        )
        sql = (
            f"SELECT {', '.join(projections)} "
            f"FROM {self.table} "
            f"WHERE {self.tenant_column} = :tenant_id "
            f"AND {self.window_column} >= :window_start "
            f"AND {self.window_column} < :window_end"
        )
        params: dict[str, Any] = {
            "tenant_id": query.tenant_id,
            "window_start": query.window_start,
            "window_end": query.window_end,
            "row_limit": max_rows,
        }
        if len(query.content_types) == 1:
            sql += f" AND {self.content_type_column} = :content_type"
            params["content_type"] = next(iter(query.content_types)).value
        if self.group_by_dimensions:
            sql += f" GROUP BY {self.news_id_column}, {self.content_type_column}"
        sql += (
            f" ORDER BY {self.news_id_column}, {self.content_type_column}"
            " LIMIT :row_limit"
        )
        return sql, params


def aggregate_metric_template(
    *,
    table: str,
    window_column: str = "event_time",
    version: str = "hot-news-metric-template-v2",
    bucket_seconds: int = 3600,
) -> HotNewsMetricSqlTemplate:
    """读取每新闻/类型/完整桶唯一一行的聚合视图；不再对小时 UV 求和。"""

    return HotNewsMetricSqlTemplate(
        table=table,
        metric_expressions={key: key for key in sorted(BASE_METRIC_KEYS)},
        window_column=window_column,
        version=version,
        bucket_seconds=bucket_seconds,
        group_by_dimensions=False,
    )


@dataclass(frozen=True, slots=True)
class SqlMetricBatch:
    """一次取数的结果与证据，用于审计和复现。"""

    snapshots: tuple[NewsMetricSnapshot, ...]
    source: Literal["template", "text2sql"]
    sql: str
    sql_hash: str
    params: Mapping[str, Any]
    request_id: str | None = None
    metric_contract_version: str = ""
    metric_contract_sha256: str = ""


class Text2SqlNewsMetricSource:
    """模板优先；模型仅能提交符合已批准指标契约的候选 SQL。"""

    def __init__(
        self,
        *,
        warehouse: SqlWarehouseClient,
        schema: Text2SqlSchema,
        template: HotNewsMetricSqlTemplate | None = None,
        generator: Text2SqlAgentRunner | None = None,
        metric_contract: HotNewsMetricSqlTemplate | None = None,
        max_rows: int | None = None,
        timeout_ms: int = 30_000,
    ) -> None:
        resolved_max_rows = max_rows if max_rows is not None else schema.max_rows
        if isinstance(resolved_max_rows, bool) or not isinstance(resolved_max_rows, int) or not 1 <= resolved_max_rows <= schema.max_rows:
            raise ValueError("max_rows must be positive and within the approved schema limit")
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")
        if template is None and generator is None:
            raise ValueError(
                "at least one of template or generator must be configured"
            )

        self.warehouse = warehouse
        self.schema = schema
        self.template = template
        self.generator = generator
        self.metric_contract = metric_contract if metric_contract is not None else template
        self.max_rows = resolved_max_rows
        self.timeout_ms = timeout_ms

        required_placeholders = frozenset(
            {"tenant_id", "window_start", "window_end"}
        )
        allowed_placeholders = set(required_placeholders) | {"row_limit", "content_type"}
        if template is not None:
            allowed_placeholders |= template.placeholders
        self._guard = SqlGuard(
            SqlGuardPolicy(
                allowed_tables=schema.table_names(),
                allowed_columns=schema.column_names(),
                tenant_column=schema.tenant_column,
                window_column=schema.window_column,
                required_placeholders=required_placeholders,
                allowed_placeholders=frozenset(allowed_placeholders),
                max_rows=self.max_rows,
                dialect=schema.dialect,
                content_type_column=self.metric_contract.content_type_column if self.metric_contract is not None else "content_type",
            )
        )

    async def fetch_snapshots(
        self,
        query: HotNewsMetricQuery,
    ) -> list[NewsMetricSnapshot]:
        batch = await self.fetch_batch(query)
        return list(batch.snapshots)

    @traced("sql.metric_source")
    async def fetch_batch(
        self,
        query: HotNewsMetricQuery,
    ) -> SqlMetricBatch:
        query.validate()
        unsupported = query.requested_metrics - (BASE_METRIC_KEYS | {"ctr"})
        if unsupported:
            raise HotNewsDataQualityError(f"unsupported snapshot metrics: {sorted(unsupported)}")
        if self.metric_contract is None:
            raise HotNewsDataQualityError("text2sql requires an approved metric contract before generation")
        requested_metrics = BASE_METRIC_KEYS
        approved_sql, approved_params = self.metric_contract.render(query, max_rows=self.max_rows)
        # Validate trusted configuration too: a malformed contract must not be
        # sent to a generator or allowed to expand the schema/scope whitelist.
        self._guard.validate(approved_sql, approved_params)

        source: Literal["template", "text2sql"] = "template"
        request_id: str | None = None

        if (
            self.template is not None
            and requested_metrics <= self.template.supported_metrics
        ):
            sql, params = self.template.render(query, max_rows=self.max_rows)
        else:
            if self.generator is None:
                raise HotNewsDataQualityError(
                    "no deterministic template covers the request and no "
                    "text2sql generator is configured"
                )
            source = "text2sql"
            result = await self.generator.run(
                self._generation_input(query, requested_metrics)
            )
            sql = result.value.sql
            request_id = result.request_id
            params = self._base_params(query)

        self._guard.validate(sql, params)
        if params != approved_params:
            raise HotNewsDataQualityError("metric query bindings differ from the approved contract")
        validate_metric_sql_contract(sql, approved_sql=approved_sql, dialect=self.schema.dialect)
        execution = await self.warehouse.execute(
            SqlQuery(
                sql=sql,
                params=params,
                timeout_ms=self.timeout_ms,
                max_rows=self.max_rows,
            )
        )
        if execution.truncated:
            raise HotNewsDataQualityError(
                "sql metric query exceeded configured row limit"
            )

        snapshots = _snapshots_from_rows(
            execution.rows,
            window_start=query.window_start,
            window_end=query.window_end,
        )
        if query.content_types and any(snapshot.content_type not in query.content_types for snapshot in snapshots):
            raise HotNewsDataQualityError("sql metric rows violate the requested content type scope")
        return SqlMetricBatch(
            snapshots=tuple(snapshots),
            source=source,
            sql=sql,
            sql_hash=sha256(sql.encode("utf-8")).hexdigest(),
            params=params,
            request_id=request_id,
            metric_contract_version=self.metric_contract.version,
            metric_contract_sha256=self.metric_contract.sha256,
        )

    def _generation_input(
        self,
        query: HotNewsMetricQuery,
        requested_metrics: frozenset[str],
    ) -> Text2SqlGenerationInput:
        return Text2SqlGenerationInput(
            dialect=self.schema.dialect,
            schema_ddl=self.schema.render_ddl(),
            metric_columns=tuple(sorted(requested_metrics)),
            content_types=tuple(
                sorted(content_type.value for content_type in query.content_types)
            ),
            ranking_limit=query.ranking_limit,
            tenant_column=self.schema.tenant_column,
            window_column=self.schema.window_column,
            required_placeholders=("tenant_id", "window_start", "window_end") + (
                ("content_type",) if len(query.content_types) == 1 else ()
            ),
            row_limit_placeholder="row_limit",
            metric_contract_version=self.metric_contract.version,
            approved_sql=self.metric_contract.render(query, max_rows=self.max_rows)[0],
        )

    def _base_params(self, query: HotNewsMetricQuery) -> dict[str, Any]:
        params: dict[str, Any] = {
            "tenant_id": query.tenant_id,
            "window_start": query.window_start,
            "window_end": query.window_end,
            "row_limit": self.max_rows,
        }
        if len(query.content_types) == 1:
            params["content_type"] = next(iter(query.content_types)).value
        return params


def _snapshots_from_rows(
    rows: tuple[Mapping[str, Any], ...],
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[NewsMetricSnapshot]:
    snapshots: list[NewsMetricSnapshot] = []
    seen: set[tuple[str, ContentType]] = set()
    for index, row in enumerate(rows):
        try:
            news_id = str(row["news_id"]).strip()
            content_type = ContentType(str(row["content_type"]).strip())
            values = {
                metric: _non_negative_int(row[metric])
                for metric in BASE_METRIC_KEYS
            }
        except (KeyError, ValueError, TypeError) as exc:
            raise HotNewsDataQualityError(
                f"sql metric row {index} is invalid: {exc}"
            ) from exc

        if not news_id:
            raise HotNewsDataQualityError(
                f"sql metric row {index} has an empty news_id"
            )

        identity = (news_id, content_type)
        if identity in seen:
            raise HotNewsDataQualityError("sql metric rows contain a duplicate news/content type identity")
        seen.add(identity)

        impressions = values["impressions"]
        clicks = values["clicks"]
        ctr = (
            Decimal(clicks) / Decimal(impressions)
            if impressions
            else Decimal("0")
        )
        snapshots.append(
            NewsMetricSnapshot(
                news_id=news_id,
                content_type=content_type,
                window_start=window_start,
                window_end=window_end,
                **values,
                ctr=ctr,
            )
        )

    snapshots.sort(
        key=lambda item: (
            item.news_id,
            item.content_type,
            item.window_start,
            item.window_end,
        )
    )
    return snapshots


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not a metric value")
    if isinstance(value, int):
        result = value
    elif isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise ValueError(f"metric value is not an integer: {value}")
        result = int(value)
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"metric value is not an integer: {value}")
        result = int(value)
    elif isinstance(value, str) and value.strip():
        result = int(value)
    else:
        raise ValueError(f"unsupported metric value: {value!r}")
    if result < 0:
        raise ValueError(f"metric value cannot be negative: {result}")
    return result
