"""模板优先、Text2SQL 兜底的热点指标取数实现。

参数化模板是默认且可复现的路径；只有当一次请求要求模板未声明的指标，或部署
没有可用模板时，才调用大模型生成候选 SQL。两条路径最终都经过 ``SqlGuard``
只读白名单校验，再送入 ``SqlWarehouseClient`` 执行，最后映射为
``NewsMetricSnapshot``，因此下游热点计算链路完全不变。
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any, Literal, Mapping

from app.analytics.entities import ContentType
from app.analytics.metrics import (
    BASE_METRIC_KEYS,
    NewsMetricSnapshot,
)
from app.analytics.metric_source import HotNewsMetricQuery
from app.analytics.sql_guard import SqlGuard, SqlGuardPolicy
from app.clients.enterprise.sql_warehouse import SqlQuery, SqlWarehouseClient
from app.domain.errors import HotNewsDataQualityError
from app.schemas.text2sql import Text2SqlGenerationInput, Text2SqlSchema
from app.services.agents.text2sql import Text2SqlAgentRunner


class TemplateNotApplicableError(HotNewsDataQualityError):
    """请求指标超出模板声明能力，需要走 Text2SQL 兜底。"""

    retryable = False


@dataclass(frozen=True, slots=True)
class HotNewsMetricSqlTemplate:
    """把结构化窗口请求渲染成固定聚合 SQL 的参数化模板。

    ``metric_expressions`` 的键是规范指标名，值是聚合 SQL 表达式。表达式必须
    来自受控配置，不能由模型生成。
    """

    table: str
    metric_expressions: Mapping[str, str]
    news_id_column: str = "news_id"
    content_type_column: str = "content_type"
    tenant_column: str = "tenant_id"
    window_column: str = "event_time"
    version: str = "hot-news-metric-template-v1"

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
        metrics = BASE_METRIC_KEYS | query.requested_metrics
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
        sql += (
            f" GROUP BY {self.news_id_column}, {self.content_type_column}"
            f" ORDER BY {self.news_id_column}, {self.content_type_column}"
            " LIMIT :row_limit"
        )
        return sql, params


def aggregate_metric_template(
    *,
    table: str,
    window_column: str = "event_time",
    version: str = "hot-news-metric-template-v1",
) -> HotNewsMetricSqlTemplate:
    """为"已聚合行为视图"构造模板：视图里已有规范指标列。"""

    return HotNewsMetricSqlTemplate(
        table=table,
        metric_expressions={key: key for key in sorted(BASE_METRIC_KEYS)},
        window_column=window_column,
        version=version,
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


class Text2SqlNewsMetricSource:
    """实现 ``NewsMetricSource``：模板优先，超纲时回退到 Text2SQL。"""

    def __init__(
        self,
        *,
        warehouse: SqlWarehouseClient,
        schema: Text2SqlSchema,
        template: HotNewsMetricSqlTemplate | None = None,
        generator: Text2SqlAgentRunner | None = None,
        max_rows: int | None = None,
        timeout_ms: int = 30_000,
    ) -> None:
        resolved_max_rows = max_rows if max_rows is not None else schema.max_rows
        if resolved_max_rows <= 0:
            raise ValueError("max_rows must be positive")
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
        self.max_rows = resolved_max_rows
        self.timeout_ms = timeout_ms

        required_placeholders = frozenset(
            {"tenant_id", "window_start", "window_end"}
        )
        allowed_placeholders = set(required_placeholders) | {"row_limit"}
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
            )
        )

    async def fetch_snapshots(
        self,
        query: HotNewsMetricQuery,
    ) -> list[NewsMetricSnapshot]:
        batch = await self.fetch_batch(query)
        return list(batch.snapshots)

    async def fetch_batch(
        self,
        query: HotNewsMetricQuery,
    ) -> SqlMetricBatch:
        query.validate()
        requested_metrics = BASE_METRIC_KEYS | query.requested_metrics

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

        self._guard.validate(sql)
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
        return SqlMetricBatch(
            snapshots=tuple(snapshots),
            source=source,
            sql=sql,
            sql_hash=sha256(sql.encode("utf-8")).hexdigest(),
            params=params,
            request_id=request_id,
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
            required_placeholders=("tenant_id", "window_start", "window_end"),
            row_limit_placeholder="row_limit",
        )

    def _base_params(self, query: HotNewsMetricQuery) -> dict[str, Any]:
        return {
            "tenant_id": query.tenant_id,
            "window_start": query.window_start,
            "window_end": query.window_end,
            "row_limit": self.max_rows,
        }


def _snapshots_from_rows(
    rows: tuple[Mapping[str, Any], ...],
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[NewsMetricSnapshot]:
    snapshots: list[NewsMetricSnapshot] = []
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
