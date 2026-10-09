"""Use the approved SQL query as a hot-news Agent metric tool.

The browser and Temporal Activity refer to the same saved query. Its rows are
candidate metrics, then Python retrieves the missing single-hour counters and
baseline from the same frozen aggregate view. Python HotNewsRanker, content
lookup, knowledge retrieval and analysis continue through the existing Ports.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any, TYPE_CHECKING
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.analytics.entities import ContentType
from app.analytics.metric_source import HotNewsMetricQuery
from app.analytics.metrics import BASE_METRIC_KEYS, NewsMetricSnapshot
from app.analytics.news_content import NewsContent
from app.analytics.ranking import MetricKey
from app.db.session import Database
from app.domain.errors import HotNewsDataQualityError
from app.knowledge.document import KnowledgeDocument
from app.knowledge.postgres_store import PostgresKnowledgeStore
from app.knowledge.native_indexing import split_text
from app.schemas.sql_assistant import SqlAssistantPreview, SqlAssistantResult
from app.services.hot_news_orchestration import HotNewsOrchestrationPolicy
from app.sql_assistant.warehouse import (
    DEMO_TENANT_ID,
    ISOLATION_TENANT_ID,
    SCHEMA_SHA256,
    SCHEMA_VERSION,
    WINDOW_END,
    WINDOW_START,
    SqlWarehouseContractError,
    SqlWarehouseTransientError,
    verify_schema_contract,
    demo_news_ids,
    warehouse_contract,
    STREAMED_PROFILES,
    dataset_window,
)
from app.sql_assistant.public_headlines import PUBLIC_HEADLINES_PROFILE, public_article

if TYPE_CHECKING:
    from app.sql_assistant.service import SqlAssistantService

ExecuteForRun = Callable[[str], Awaitable[SqlAssistantResult]]
DEMO_NEWS_IDS = tuple(f"demo-news-{index:03d}" for index in range(1, 13))
SCALED_PROFILE = "enterprise-v2"
CONTENT_BATCH_SIZE = 200

METADATA_SQL = """SELECT tenant_id, news_id, title, content_type,
    category, source, publish_time
FROM dw.dim_news
WHERE tenant_id = :tenant_id AND news_id = ANY(:news_ids)
ORDER BY news_id
LIMIT :row_limit"""

_KNOWLEDGE_STATE_SQL = """SELECT d.tenant_id, d.document_id, d.news_id,
    d.content_version, d.content_sha256, d.embedding_version,
    d.title, d.body, d.metadata, COUNT(c.chunk_id) AS chunk_count
FROM native_knowledge_documents AS d
LEFT JOIN native_knowledge_chunks AS c
  ON c.tenant_id = d.tenant_id AND c.document_id = d.document_id
WHERE d.tenant_id = :tenant_id AND d.document_id = ANY(:document_ids)
GROUP BY d.tenant_id, d.document_id
ORDER BY d.document_id
LIMIT :row_limit"""

_KNOWLEDGE_CHUNKS_SQL = """SELECT tenant_id, document_id, chunk_id,
    news_id, content_version, chunk_index, embedding_version,
    tier, title, excerpt, source_url, publish_time,
    CASE WHEN jsonb_typeof(vector) = 'array'
      THEN jsonb_array_length(vector) ELSE 0 END AS vector_dimensions
FROM native_knowledge_chunks
WHERE tenant_id = :tenant_id AND document_id = ANY(:document_ids)
ORDER BY document_id, chunk_index
LIMIT :row_limit"""

# This supplementary tool SQL is trusted Python code, never a model/browser
# argument. The public guard remains restricted to ranking/trend projections.
# A single bucket provides actual UV without illegally summing hourly UV.
SUPPLEMENTAL_SQL = """SELECT tenant_id, news_id, event_time, title, content_type,
    category, source, publish_time, impressions, clicks, unique_users,
    total_duration_seconds, effective_consumptions, interactions,
    baseline_impressions, baseline_clicks,
    baseline_effective_consumptions, baseline_interactions
FROM dw.news_behavior_aggregate
WHERE tenant_id = :tenant_id
  AND event_time >= :window_start
  AND event_time < :window_end
  AND news_id = ANY(:news_ids)
ORDER BY news_id, event_time
LIMIT :row_limit"""


def _aware_time(value: Any) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
        raise HotNewsDataQualityError("SQL hot-news window must be timezone aware")
    return result


def _validate_window(start: datetime, end: datetime, profile: str = "classic-v1") -> None:
    if end - start != timedelta(hours=1):
        raise HotNewsDataQualityError("SQL hot-news simulation requires exactly one hourly bucket; hourly UV cannot be summed")
    if start.minute or start.second or start.microsecond or end.minute or end.second or end.microsecond:
        raise HotNewsDataQualityError("SQL hot-news simulation window must align to full hours")
    sample_start, sample_end = dataset_window(profile)
    if start < sample_start or end > sample_end:
        raise HotNewsDataQualityError("SQL hot-news simulation window is outside the frozen synthetic dataset")


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise HotNewsDataQualityError(f"SQL hot-news {name} cannot be boolean")
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise HotNewsDataQualityError(f"SQL hot-news {name} is not a metric") from exc
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise HotNewsDataQualityError(f"SQL hot-news {name} must be a nonnegative integer")
    return int(number)


def _decimal(value: Any, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise HotNewsDataQualityError(f"SQL hot-news baseline {name} is not a metric") from exc
    if not number.is_finite() or number < 0:
        raise HotNewsDataQualityError(f"SQL hot-news baseline {name} must be finite and nonnegative")
    return number


@dataclass(frozen=True, slots=True)
class SqlNewsMetricBaseline:
    """Known baseline fields, explicitly missing UV and duration reference."""

    news_id: str
    content_type: ContentType
    sample_count: int
    impressions: Decimal
    clicks: Decimal
    effective_consumptions: Decimal
    interactions: Decimal
    ctr: Decimal
    unique_users: None = None
    total_duration_seconds: None = None
    reference_version: str = "synthetic-hourly-baseline-v1"


class SqlHotNewsDetailRepository:
    """A fixed read-only tool for a bounded, authenticated local fixture scope."""

    def __init__(self, database: Database, *, tenant_id: str,
                 dataset_profile: str = "classic-v1", news_per_tenant: int = 1200) -> None:
        self.database = database
        self.tenant_id = str(UUID(tenant_id))
        if UUID(self.tenant_id) not in {DEMO_TENANT_ID, ISOLATION_TENANT_ID}:
            raise HotNewsDataQualityError("SQL hot-news fixtures do not exist for this tenant")
        self.dataset_profile = dataset_profile
        self.contract = warehouse_contract(dataset_profile)
        self.news_ids = demo_news_ids(dataset_profile, news_per_tenant=news_per_tenant)
        self._allowed_ids = frozenset(self.news_ids)

    async def fetch_details(
        self, *, window_start: datetime, window_end: datetime, news_ids: tuple[str, ...]
    ) -> tuple[Mapping[str, Any], ...]:
        verify_schema_contract(self.dataset_profile)
        _validate_window(window_start, window_end, self.dataset_profile)
        requested = tuple(dict.fromkeys(news_ids))
        if not set(requested) <= self._allowed_ids:
            raise HotNewsDataQualityError("SQL hot-news requested an unknown synthetic news_id")
        if not requested:
            return ()
        params = {
            "tenant_id": UUID(self.tenant_id), "window_start": window_start,
            "window_end": window_end, "news_ids": list(requested), "row_limit": len(requested) + 1,
        }
        try:
            async with self.database.engine.connect() as connection:
                async with connection.begin():
                    await connection.execute(text("SET TRANSACTION READ ONLY"))
                    await connection.execute(text("SELECT set_config('statement_timeout', '10000', true)"))
                    rows = (await connection.execute(text(SUPPLEMENTAL_SQL), params)).mappings().all()
        except DBAPIError as exc:
            state = getattr(exc.orig, "sqlstate", "") or ""
            if exc.connection_invalidated or state.startswith("08") or state in {"40001", "40P01", "57014", "57P01", "57P02", "57P03"}:
                raise SqlWarehouseTransientError("SQL hot-news detail tool temporarily unavailable") from exc
            raise SqlWarehouseContractError("SQL hot-news detail tool rejected its fixed query") from exc
        except SQLAlchemyError as exc:
            raise SqlWarehouseTransientError("SQL hot-news detail connection failed") from exc
        if len(rows) > len(requested):
            raise HotNewsDataQualityError("SQL hot-news single-hour detail tool returned duplicate buckets")
        seen: set[str] = set()
        for row in rows:
            news_id = str(row["news_id"])
            event_time = _aware_time(row["event_time"])
            if str(row["tenant_id"]) != self.tenant_id or news_id not in requested or news_id in seen:
                raise HotNewsDataQualityError("SQL hot-news detail identity/scope mismatch")
            if event_time != window_start:
                raise HotNewsDataQualityError("SQL hot-news detail bucket differs from the approved hour")
            seen.add(news_id)
        if seen != set(requested):
            raise HotNewsDataQualityError("SQL hot-news detail tool lacks one or more requested news buckets")
        return tuple(dict(row) for row in rows)

    async def fetch_metadata(self, *, news_ids: tuple[str, ...]) -> tuple[Mapping[str, Any], ...]:
        """Read DIM metadata independently of hour metrics, in bounded batches."""
        verify_schema_contract(self.dataset_profile)
        requested = tuple(dict.fromkeys(news_ids))
        if not set(requested) <= self._allowed_ids:
            raise HotNewsDataQualityError("SQL hot-news requested an unknown synthetic news_id")
        collected = []
        for offset in range(0, len(requested), CONTENT_BATCH_SIZE):
            batch = requested[offset:offset + CONTENT_BATCH_SIZE]
            params = {"tenant_id": UUID(self.tenant_id), "news_ids": list(batch), "row_limit": len(batch) + 1}
            try:
                async with self.database.engine.connect() as connection:
                    async with connection.begin():
                        await connection.execute(text("SET TRANSACTION READ ONLY"))
                        await connection.execute(text("SELECT set_config('statement_timeout', '10000', true)"))
                        rows = (await connection.execute(text(METADATA_SQL), params)).mappings().all()
            except SQLAlchemyError as exc:
                raise SqlWarehouseTransientError("SQL hot-news metadata query failed") from exc
            seen = set()
            for row in rows:
                news_id = str(row["news_id"])
                if (str(row["tenant_id"]) != self.tenant_id or news_id not in batch or news_id in seen):
                    raise HotNewsDataQualityError("SQL hot-news metadata identity/scope mismatch")
                ContentType(str(row["content_type"]))
                _aware_time(row["publish_time"])
                seen.add(news_id)
            if seen != set(batch):
                raise HotNewsDataQualityError("SQL hot-news metadata lacks requested news IDs")
            collected.extend(dict(row) for row in rows)
        return tuple(collected)


class SqlHotNewsContentRepository:
    """Synthetic content tied to metadata and IDs in the same local warehouse."""

    def __init__(self, details: SqlHotNewsDetailRepository) -> None:
        self.details = details

    async def batch_get_by_news_ids(self, *, tenant_id: str, news_ids: tuple[str, ...]) -> dict[str, NewsContent]:
        if str(UUID(tenant_id)) != self.details.tenant_id:
            raise HotNewsDataQualityError("SQL hot-news content request crossed its bound tenant")
        requested = tuple(dict.fromkeys(news_ids))
        if getattr(self.details, "dataset_profile", "classic-v1") in STREAMED_PROFILES:
            rows = await self.details.fetch_metadata(news_ids=requested)
        else:
            requested = tuple(item for item in requested if item in DEMO_NEWS_IDS)
            rows = await self.details.fetch_details(
                window_start=dataset_window(getattr(self.details, "dataset_profile", "classic-v1"))[0],
                window_end=dataset_window(getattr(self.details, "dataset_profile", "classic-v1"))[0] + timedelta(hours=1), news_ids=requested,
            )
        contents: dict[str, NewsContent] = {}
        for row in rows:
            title = str(row["title"])
            category = str(row["category"])
            public = getattr(self.details, "dataset_profile", "classic-v1") == PUBLIC_HEADLINES_PROFILE
            article = public_article(str(row["news_id"])) if public else None
            if article is not None and (title != article["title"] or row["source"] != article["source"]
                                        or category != article["category"]
                                        or row["content_type"] != article["content_type"]
                                        or _aware_time(row["publish_time"]) != _aware_time(article["published_at"])):
                raise HotNewsDataQualityError("public headline metadata differs from its frozen catalog")
            summary = (
                f"公开新闻标题引用：{title}。栏目为{category}，公开来源为{row['source']}。"
                "这里只保存公开标题及出处，没有抓取或重建原文；小时指标和基线是本地合成运营样本。"
                "标题采集日期与模拟行为日期独立，不能据此判断真实新闻热度、原因或事实。"
            ) if public else (
                f"本地合成新闻样本：{title}。栏目为{category}，来源为{row['source']}。"
                "用于演示从小时指标筛选、Python热度排行到正文与关联证据分析的完整链路。"
                "这里没有企业真实正文，合成内容不用于新闻事实判断。"
            )
            body = summary + f" 该样本的统一新闻标识为{row['news_id']}；数仓、内容和知识索引通过此标识关联。"
            content = NewsContent(
                news_id=str(row["news_id"]), title=title, summary=summary, body=body,
                content_type=ContentType(str(row["content_type"])),
                publish_time=_aware_time(row["publish_time"]),
                source_url=article["url"] if article is not None else f"https://example.invalid/local-news/{row['news_id']}",
            )
            content.validate()
            contents[content.news_id] = content
        return contents


class SqlAssistantHotNewsMetricSource:
    """A NewsMetricSource and BaselineProvider bound to one saved SQL tool call."""

    def __init__(
        self, *, sql_service: SqlAssistantService, preview: SqlAssistantPreview,
        tenant_id: str, user_id: str, production_bundle_version: str,
        details: SqlHotNewsDetailRepository, execute_for_run: ExecuteForRun | None = None,
    ) -> None:
        self.sql_service = sql_service
        self.preview = preview
        self.tenant_id = str(UUID(tenant_id))
        self.user_id = user_id
        self.production_bundle_version = production_bundle_version
        self.details = details
        self.contract = getattr(details, "contract", warehouse_contract())
        self.dataset_profile = getattr(details, "dataset_profile", "classic-v1")
        self.execute_for_run = execute_for_run
        self.window_start = _aware_time(preview.parameters["window_start"])
        self.window_end = _aware_time(preview.parameters["window_end"])
        _validate_window(self.window_start, self.window_end, self.dataset_profile)
        if str(preview.parameters["tenant_id"]) != self.tenant_id:
            raise HotNewsDataQualityError("SQL hot-news preview tenant mismatch")
        self._snapshots: tuple[NewsMetricSnapshot, ...] | None = None
        self._baselines: dict[MetricKey, SqlNewsMetricBaseline] = {}
        self._result: SqlAssistantResult | None = None

    def _validate_scope(self, query: HotNewsMetricQuery) -> None:
        query.validate()
        if query.tenant_id != self.tenant_id or query.window_start != self.window_start or query.window_end != self.window_end:
            raise HotNewsDataQualityError("SQL hot-news tool cannot change its saved tenant/window scope")
        if query.requested_metrics - (BASE_METRIC_KEYS | {"ctr"}):
            raise HotNewsDataQualityError("SQL hot-news tool cannot provide unapproved extra metrics")

    async def fetch_snapshots(self, query: HotNewsMetricQuery) -> list[NewsMetricSnapshot]:
        self._validate_scope(query)
        if self._snapshots is not None:
            if query.content_types and any(item.content_type not in query.content_types for item in self._snapshots):
                raise HotNewsDataQualityError("cached SQL hot-news content types differ from policy")
            return list(self._snapshots)
        result = (
            await self.execute_for_run(self.preview.query_id)
            if self.execute_for_run is not None
            else await self.sql_service.execute(self.preview.query_id, tenant_id=self.tenant_id, user_id=self.user_id)
        )
        if result.query_id != self.preview.query_id or result.sql_hash != self.preview.sql_hash or result.truncated:
            raise HotNewsDataQualityError("SQL hot-news tool output does not match its approved query")
        if result.row_count != len(result.rows) or len(result.rows) > int(self.preview.parameters["row_limit"]):
            raise HotNewsDataQualityError("SQL hot-news tool returned an invalid candidate count")
        news_ids = tuple(str(row["news_id"]) for row in result.rows)
        if len(set(news_ids)) != len(news_ids):
            raise HotNewsDataQualityError("SQL hot-news tool returned duplicate candidate news_ids")
        detail_rows = await self.details.fetch_details(
            window_start=self.window_start, window_end=self.window_end, news_ids=news_ids,
        )
        detail_by_id = {str(row["news_id"]): row for row in detail_rows}
        snapshots: list[NewsMetricSnapshot] = []
        baselines: dict[MetricKey, SqlNewsMetricBaseline] = {}
        for candidate in result.rows:
            news_id = str(candidate["news_id"])
            row = detail_by_id[news_id]
            content_type = ContentType(str(row["content_type"]))
            if str(candidate["content_type"]) != content_type.value or candidate["title"] != row["title"]:
                raise HotNewsDataQualityError("SQL hot-news candidate metadata differs from the same warehouse bucket")
            if query.content_types and content_type not in query.content_types:
                raise HotNewsDataQualityError("SQL hot-news candidate content type is outside policy")
            values = {name: _integer(row[name], name) for name in BASE_METRIC_KEYS}
            for name in ("impressions", "clicks", "effective_consumptions", "interactions"):
                if _integer(candidate[name], name) != values[name]:
                    raise HotNewsDataQualityError("SQL candidate metrics differ from the authoritative same-hour detail")
            if values["clicks"] > values["impressions"] or values["effective_consumptions"] > values["clicks"] or values["unique_users"] > values["clicks"]:
                raise HotNewsDataQualityError("SQL hot-news bucket metrics violate counter relationships")
            ctr = Decimal(values["clicks"]) / Decimal(values["impressions"]) if values["impressions"] else Decimal(0)
            snapshots.append(NewsMetricSnapshot(
                news_id=news_id, content_type=content_type, window_start=self.window_start,
                window_end=self.window_end, ctr=ctr, **values,
            ))
            baseline_values = {
                name: _decimal(row[f"baseline_{name}"], name)
                for name in ("impressions", "clicks", "effective_consumptions", "interactions")
            }
            baselines[(news_id, content_type)] = SqlNewsMetricBaseline(
                news_id=news_id, content_type=content_type, sample_count=1,
                ctr=baseline_values["clicks"] / baseline_values["impressions"] if baseline_values["impressions"] else Decimal(0),
                reference_version=("synthetic-hourly-baseline-v4" if self.dataset_profile == "timeline-v4" else
                                   "synthetic-hourly-baseline-v3" if self.dataset_profile == PUBLIC_HEADLINES_PROFILE
                                   else "synthetic-hourly-baseline-v2" if self.dataset_profile == SCALED_PROFILE
                                   else "synthetic-hourly-baseline-v1"),
                **baseline_values,
            )
        snapshots.sort(key=lambda item: (item.news_id, item.content_type.value))
        self._snapshots = tuple(snapshots)
        self._baselines = baselines
        self._result = result
        return list(snapshots)

    async def get_baselines(
        self, *, tenant_id: str, window_start: datetime, window_end: datetime,
        production_bundle_version: str, metric_keys: frozenset[MetricKey],
    ) -> dict[MetricKey, SqlNewsMetricBaseline]:
        if tenant_id != self.tenant_id or window_start != self.window_start or window_end != self.window_end or production_bundle_version != self.production_bundle_version:
            raise HotNewsDataQualityError("SQL hot-news baseline scope/bundle mismatch")
        if self._snapshots is None:
            raise HotNewsDataQualityError("SQL hot-news baseline requested before its metric tool completed")
        if not metric_keys <= set(self._baselines):
            raise HotNewsDataQualityError("SQL hot-news baseline contains unknown candidate keys")
        return {key: self._baselines[key] for key in metric_keys}

    async def get_tool_trace(self) -> dict[str, Any]:
        scope = {
            "query_id": self.preview.query_id, "tenant_id": self.tenant_id,
            "window_start": self.window_start.isoformat(), "window_end": self.window_end.isoformat(),
            "schema_sha256": self.contract.sha256, "sql_hash": self.preview.sql_hash,
        }
        return {
            "query_id": self.preview.query_id,
            "preview": self.preview.model_dump(mode="json"),
            "result": self._result.model_dump(mode="json") if self._result is not None else None,
            "scope_sha256": sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "supplemental_sql": SUPPLEMENTAL_SQL,
            "supplemental_sql_hash": sha256(SUPPLEMENTAL_SQL.encode()).hexdigest(),
            "metric_mapping_notes": [
                "SQL 查询挑选候选新闻；Python HotNewsRanker 重新计算权威热度与最终排行。",
                "窗口严格限定一个小时桶：unique_users 使用该桶真实去重值，禁止跨小时求和。",
                "候选计数与同一聚合视图交叉校验；时长、小时 UV 与基线由固定 Python 参数化 SQL 补充。",
                "基线的 UV 与时长未提供，保持 null；sample_count=1 表示一份合成参考，不代表推断出的历史样本数量。",
                "SQL 演示热度仅用于候选筛选；热点 Agent 仍采用原有 Python 四分量热度公式。",
            ],
        }


@dataclass(frozen=True, slots=True)
class NativeHotNewsSqlDependencies:
    metric_source: SqlAssistantHotNewsMetricSource
    baseline_provider: SqlAssistantHotNewsMetricSource
    content_repository: SqlHotNewsContentRepository
    policy: HotNewsOrchestrationPolicy
    tenant_id: str
    window_start: datetime
    window_end: datetime
    production_bundle_version: str
    news_ids: tuple[str, ...]
    query_id: str
    dataset_profile: str = "classic-v1"


async def build_native_hot_news_sql_dependencies(
    *, database: Database, sql_service: SqlAssistantService, query_id: str,
    tenant_id: str, user_id: str, production_bundle_version: str = "sql-hot-news-v1",
    execute_for_run: ExecuteForRun | None = None,
) -> NativeHotNewsSqlDependencies:
    profile = getattr(sql_service, "dataset_profile", "classic-v1")
    contract = warehouse_contract(profile)
    verify_schema_contract(profile)
    snapshot = await sql_service.store.get(query_id, tenant_id, user_id)
    if snapshot is None:
        raise HotNewsDataQualityError("SQL hot-news query does not belong to this tenant/user")
    if snapshot.get("tenant_id") != tenant_id or snapshot.get("user_id") != user_id:
        raise HotNewsDataQualityError("SQL hot-news saved query owner mismatch")
    if snapshot.get("scenario", {}).get("result_mode") != "ranking":
        raise HotNewsDataQualityError("SQL hot-news metric tool requires news candidates, not cross-news hourly trends")
    preview = SqlAssistantPreview.model_validate(snapshot["preview"])
    if preview.query_id != query_id or preview.schema_version != contract.version or preview.schema_sha256 != contract.sha256:
        raise HotNewsDataQualityError("SQL hot-news saved query schema/id mismatch")
    details = SqlHotNewsDetailRepository(database, tenant_id=tenant_id, dataset_profile=profile,
                                       news_per_tenant=getattr(sql_service, "news_per_tenant", 1200))
    source = SqlAssistantHotNewsMetricSource(
        sql_service=sql_service, preview=preview, tenant_id=tenant_id, user_id=user_id,
        production_bundle_version=production_bundle_version, details=details, execute_for_run=execute_for_run,
    )
    content_type = preview.parameters.get("content_type")
    policy = HotNewsOrchestrationPolicy(
        production_bundle_version=production_bundle_version,
        content_types=frozenset({ContentType(str(content_type))}) if content_type is not None else frozenset({ContentType.ARTICLE, ContentType.VIDEO}),
        ranking_limit=int(preview.parameters["row_limit"]), related_limit=3, candidate_limit=20,
    )
    policy.validate()
    news_ids = details.news_ids
    if profile in STREAMED_PROFILES:
        if policy.ranking_limit > 100:
            raise HotNewsDataQualityError("SQL hot-news scaled run exceeds 100 candidate budget")
        snapshots = await source.fetch_snapshots(HotNewsMetricQuery(
            tenant_id=tenant_id, window_start=source.window_start, window_end=source.window_end,
            content_types=policy.content_types, ranking_limit=policy.ranking_limit,
        ))
        news_ids = tuple(item.news_id for item in snapshots)
    return NativeHotNewsSqlDependencies(
        metric_source=source, baseline_provider=source,
        content_repository=SqlHotNewsContentRepository(details), policy=policy,
        tenant_id=tenant_id, window_start=source.window_start, window_end=source.window_end,
        production_bundle_version=production_bundle_version, news_ids=news_ids, query_id=query_id,
        dataset_profile=profile,
    )


async def seed_native_sql_knowledge(dependencies: NativeHotNewsSqlDependencies, *, store: PostgresKnowledgeStore) -> None:
    contents = await dependencies.content_repository.batch_get_by_news_ids(
        tenant_id=dependencies.tenant_id, news_ids=dependencies.news_ids,
    )
    if set(contents) != set(dependencies.news_ids):
        raise HotNewsDataQualityError("SQL hot-news knowledge seed does not match approved news IDs")
    profile = getattr(dependencies, "dataset_profile", "classic-v1")
    contract = warehouse_contract(profile)
    if profile not in STREAMED_PROFILES and set(dependencies.news_ids) != set(DEMO_NEWS_IDS):
        raise HotNewsDataQualityError("SQL hot-news knowledge seed does not match the 12 warehouse news IDs")
    documents = tuple(KnowledgeDocument(
        document_id=f"news:{news_id}", title=contents[news_id].title,
        text=contents[news_id].body, media_type=contents[news_id].content_type.value,
        text_source=("synthetic-sql-warehouse-v4" if profile == "timeline-v4" else
                     "public-headline-reference-v3" if profile == PUBLIC_HEADLINES_PROFILE else
                     "synthetic-sql-warehouse-v2" if profile == SCALED_PROFILE else "synthetic-sql-warehouse-v1"),
        source_url=contents[news_id].source_url,
        metadata={"news_id": news_id, "content_version": "1", "warehouse_schema_version": contract.version,
                  "publish_time": contents[news_id].publish_time.isoformat(),
                  **({"warehouse_schema_sha256": contract.sha256} if profile in STREAMED_PROFILES else {})},
    ) for news_id in dependencies.news_ids)
    if profile in STREAMED_PROFILES:
        if len(documents) > 100:
            raise HotNewsDataQualityError("SQL hot-news knowledge seed exceeds candidate budget")
        # Shared store belongs to this one local worker. The database preflight
        # remains authoritative after every restart; the lock only avoids two
        # overlapping local runs embedding the same missing document twice.
        lock = getattr(store, "_synthetic_sql_seed_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            setattr(store, "_synthetic_sql_seed_lock", lock)
        async with lock:
            await _seed_scaled_sql_knowledge(dependencies, documents=documents, store=store)
        return
    report = await store.upsert_documents(tenant_id=dependencies.tenant_id, documents=documents)
    if not report.is_clean or len(report.outcomes) != len(documents):
        raise HotNewsDataQualityError("SQL hot-news knowledge index did not become ready")


async def _knowledge_preflight(database, *, tenant_id, documents, store) -> tuple[str, ...]:
    """Check persisted content and ready chunks before any embedding happens."""
    if not documents:
        return ()
    expected = {document.document_id: document for document in documents}
    params = {"tenant_id": tenant_id, "document_ids": list(expected), "row_limit": len(expected) + 1}
    async with database.engine.connect() as connection:
        async with connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            await connection.execute(text("SELECT set_config('statement_timeout', '10000', true)"))
            states = (await connection.execute(text(_KNOWLEDGE_STATE_SQL), params)).mappings().all()
            seen = set()
            parts_by_id = {}
            state_by_id = {}
            missing = set(expected)
            for state in states:
                identifier = state["document_id"]
                if state["tenant_id"] != tenant_id or identifier not in expected or identifier in seen:
                    raise HotNewsDataQualityError("SQL knowledge state identity mismatch")
                seen.add(identifier)
                document = expected[identifier]
                wanted_version = int(document.metadata["content_version"])
                old_version = state["content_version"]
                wanted_hash = sha256(f"{document.title}\x1f{document.text}".encode()).hexdigest()
                if state["news_id"] != document.metadata["news_id"] or type(old_version) is not int or old_version < 1 or old_version > wanted_version:
                    raise HotNewsDataQualityError("SQL knowledge content version/identity mismatch")
                if old_version == wanted_version and (
                    state["content_sha256"] != wanted_hash or state["title"] != document.title
                    or state["body"] != document.text or state["metadata"] != dict(document.metadata)
                ):
                    raise HotNewsDataQualityError("SQL knowledge changed content requires a newer version")
                parts = split_text(state["body"], max_chars=getattr(store, "_chunk_chars", 800),
                                   overlap=getattr(store, "_overlap", 80))
                if state["chunk_count"] != len(parts) or not parts:
                    raise HotNewsDataQualityError("SQL knowledge ready chunks are missing")
                parts_by_id[identifier] = parts
                state_by_id[identifier] = state
                if old_version == wanted_version and state["embedding_version"] == store.embedding_version:
                    missing.discard(identifier)
            if state_by_id:
                params = {"tenant_id": tenant_id, "document_ids": list(state_by_id),
                          "row_limit": sum(len(parts) for parts in parts_by_id.values()) + 1}
                chunks = (await connection.execute(text(_KNOWLEDGE_CHUNKS_SQL), params)).mappings().all()
                seen_chunks = set()
                for chunk in chunks:
                    identifier = chunk["document_id"]
                    state = state_by_id.get(identifier)
                    index = chunk["chunk_index"]
                    if (chunk["tenant_id"] != tenant_id or state is None or type(index) is not int
                            or not 0 <= index < len(parts_by_id[identifier])
                            or (identifier, index) in seen_chunks):
                        raise HotNewsDataQualityError("SQL knowledge chunk identity mismatch")
                    document = expected[identifier]
                    expected_chunk_id = sha256(
                        f"{tenant_id}\x1f{identifier}\x1f{state['content_version']}\x1f{index}\x1f{state['embedding_version']}".encode()
                    ).hexdigest()
                    if (chunk["chunk_id"] != expected_chunk_id or chunk["news_id"] != state["news_id"]
                            or chunk["content_version"] != state["content_version"]
                            or chunk["embedding_version"] != state["embedding_version"]
                            or chunk["tier"] != store.tier.value or chunk["title"] != state["title"]
                            or chunk["excerpt"] != parts_by_id[identifier][index][:1000]
                            or chunk["source_url"] != (document.source_url or None)
                            or _aware_time(chunk["publish_time"]) != _aware_time(document.metadata["publish_time"])
                            or type(chunk["vector_dimensions"]) is not int or chunk["vector_dimensions"] < 1):
                        raise HotNewsDataQualityError("SQL knowledge ready chunk version/content mismatch")
                    seen_chunks.add((identifier, index))
                wanted_chunks = {(identifier, index) for identifier, parts in parts_by_id.items() for index in range(len(parts))}
                if seen_chunks != wanted_chunks:
                    raise HotNewsDataQualityError("SQL knowledge ready chunks are missing")
    return tuple(identifier for identifier in expected if identifier in missing)


async def _seed_scaled_sql_knowledge(dependencies, *, documents, store):
    database = dependencies.content_repository.details.database
    missing = await _knowledge_preflight(database, tenant_id=dependencies.tenant_id, documents=documents, store=store)
    pending = tuple(document for document in documents if document.document_id in missing)
    if pending:
        report = await store.upsert_documents(tenant_id=dependencies.tenant_id, documents=pending)
        if not report.is_clean or len(report.outcomes) != len(pending):
            raise HotNewsDataQualityError("SQL hot-news knowledge index did not become ready")
    if await _knowledge_preflight(database, tenant_id=dependencies.tenant_id, documents=documents, store=store):
        raise HotNewsDataQualityError("SQL hot-news knowledge index did not become ready")
