"""用本地 Postgres 聚合表演示真实 Text2SQL -> 热点分析链路。

运行方式（在 writing-agent-service 目录）：
    python -m examples.hot_news_sql_warehouse_demo

默认路径需要本地 ``.env`` 配置数据库、``FASTGPT_TEXT2SQL_APP_ID`` 和
``FASTGPT_HOT_NEWS_APP_ID``；没有 Text2SQL App 时可显式使用
``--template-only``。该脚本只写入 ``dw.news_behavior_aggregate`` 中的固定
模拟租户样例，并以只读事务执行通过护栏的 SELECT。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any, Mapping
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.analytics.metric_source import HotNewsMetricQuery
from app.analytics.metrics import BASE_METRIC_KEYS, NewsMetricSnapshot
from app.analytics.text2sql_metric_source import (
    HotNewsMetricSqlTemplate,
    SqlMetricBatch,
    Text2SqlNewsMetricSource,
)
from app.clients.enterprise.sql_warehouse import (
    SqlQuery,
    SqlQueryResult,
    SqlWarehouseClient,
)
from app.clients.fastgpt import FastGPTClient
from app.clients.knowledge_base import RelatedNews, RelatedNewsSearchQuery
from app.config import Settings, get_settings
from app.db.session import Database
from app.domain.errors import HotNewsDataQualityError
from app.hot_news_bootstrap import (
    create_hot_news_runtime,
    create_text2sql_metric_source,
)
from app.schemas.text2sql import Text2SqlColumn, Text2SqlSchema, Text2SqlTable
from app.services.hot_news_run_store import PostgresHotNewsRunStore
from app.services.hot_news_orchestration import HotNewsRunRequest
from examples.hot_news_e2e_support import build_hot_news_e2e_dependencies
from examples.hot_news_demo import load_scenario


DEMO_TABLE = "dw.news_behavior_aggregate"
DEMO_TENANT_ID = "11111111-1111-4111-8111-111111111111"
DEMO_BUNDLE_VERSION = "local-sql-text2sql-demo-v1"
DEMO_WORKFLOW_VERSION = "hot-news-sql-warehouse-demo-v1"

_CREATE_SCHEMA = "CREATE SCHEMA IF NOT EXISTS dw"
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS dw.news_behavior_aggregate (
    tenant_id varchar(64) NOT NULL,
    event_time timestamptz NOT NULL,
    news_id varchar(128) NOT NULL,
    news_title text NOT NULL,
    content_type varchar(24) NOT NULL,
    channel varchar(64) NOT NULL,
    impressions bigint NOT NULL CHECK (impressions >= 0),
    clicks bigint NOT NULL CHECK (clicks >= 0),
    unique_users bigint NOT NULL CHECK (unique_users >= 0),
    total_duration_seconds bigint NOT NULL CHECK (total_duration_seconds >= 0),
    effective_consumptions bigint NOT NULL CHECK (effective_consumptions >= 0),
    interactions bigint NOT NULL CHECK (interactions >= 0),
    PRIMARY KEY (tenant_id, event_time, news_id, content_type)
)
"""
_UPSERT_ROWS = text("""
INSERT INTO dw.news_behavior_aggregate (
    tenant_id, event_time, news_id, news_title, content_type, channel,
    impressions, clicks, unique_users, total_duration_seconds,
    effective_consumptions, interactions
) VALUES (
    :tenant_id, :event_time, :news_id, :news_title, :content_type, :channel,
    :impressions, :clicks, :unique_users, :total_duration_seconds,
    :effective_consumptions, :interactions
)
ON CONFLICT (tenant_id, event_time, news_id, content_type) DO UPDATE SET
    news_title = EXCLUDED.news_title,
    channel = EXCLUDED.channel,
    impressions = EXCLUDED.impressions,
    clicks = EXCLUDED.clicks,
    unique_users = EXCLUDED.unique_users,
    total_duration_seconds = EXCLUDED.total_duration_seconds,
    effective_consumptions = EXCLUDED.effective_consumptions,
    interactions = EXCLUDED.interactions
""")


def build_text2sql_schema() -> Text2SqlSchema:
    """向 Text2SQL Agent 公开聚合表结构，不包含表内数据。"""

    columns = (
        Text2SqlColumn(
            name="tenant_id",
            data_type="varchar(64)",
            description="租户隔离键，必须使用 :tenant_id 参数过滤",
        ),
        Text2SqlColumn(
            name="event_time",
            data_type="timestamptz",
            description="聚合窗口起点，使用左闭右开窗口过滤",
        ),
        Text2SqlColumn(
            name="news_id", data_type="varchar(128)", description="新闻 ID"
        ),
        Text2SqlColumn(
            name="news_title", data_type="text", description="模拟新闻标题"
        ),
        Text2SqlColumn(
            name="content_type", data_type="varchar(24)", description="article 或 video"
        ),
        Text2SqlColumn(
            name="channel", data_type="varchar(64)", description="模拟新闻频道"
        ),
        Text2SqlColumn(name="impressions", data_type="bigint", description="窗口曝光量"),
        Text2SqlColumn(name="clicks", data_type="bigint", description="窗口点击量"),
        Text2SqlColumn(name="unique_users", data_type="bigint", description="模拟独立用户估值"),
        Text2SqlColumn(
            name="total_duration_seconds",
            data_type="bigint",
            description="总消费时长（秒）",
        ),
        Text2SqlColumn(
            name="effective_consumptions",
            data_type="bigint",
            description="有效阅读或播放量",
        ),
        Text2SqlColumn(
            name="interactions", data_type="bigint", description="点赞等互动量"
        ),
    )
    return Text2SqlSchema(
        dialect="postgres",
        tables=(
            Text2SqlTable(
                name=DEMO_TABLE,
                description="本地演示用的新闻窗口级聚合指标；无用户级明细",
                columns=columns,
            ),
        ),
        tenant_column="tenant_id",
        window_column="event_time",
        max_rows=1000,
    )


def build_demo_metric_template() -> HotNewsMetricSqlTemplate:
    """每个新闻/窗口可能有多条聚合切片，使用 SUM 归并成规范快照。"""

    return HotNewsMetricSqlTemplate(
        table=DEMO_TABLE,
        metric_expressions={
            metric: f"SUM({metric})" for metric in sorted(BASE_METRIC_KEYS)
        },
        window_column="event_time",
        version="local-sql-demo-template-v1",
    )


def build_demo_rows(
    scenario: Mapping[str, Any],
    *,
    tenant_id: str = DEMO_TENANT_ID,
) -> list[dict[str, Any]]:
    """将仓库内热点场景转换成窗口级模拟指标行。"""

    if not tenant_id.strip():
        raise ValueError("tenant_id cannot be empty")
    event_time = datetime.fromisoformat(
        str(scenario["current_window_start"]).replace("Z", "+00:00")
    )
    rows: list[dict[str, Any]] = []
    for news in scenario["news"]:
        metrics = news["current"]
        clicks = int(metrics["clicks"])
        effective = int(metrics["effective_consumptions"])
        rows.append(
            {
                "tenant_id": tenant_id,
                "event_time": event_time,
                "news_id": str(news["news_id"]),
                "news_title": str(news["title"]),
                "content_type": str(news["content_type"]),
                "channel": str(news.get("channel") or "unknown"),
                "impressions": int(metrics["impressions"]),
                "clicks": clicks,
                # The source fixture has no user-level identities. This clearly
                # synthetic estimate is only for exercising aggregate schemas.
                "unique_users": max(clicks, effective),
                "total_duration_seconds": int(metrics["duration_seconds"])
                * effective,
                "effective_consumptions": effective,
                "interactions": int(metrics["interactions"]),
            }
        )
    return rows


async def seed_demo_warehouse(engine: AsyncEngine) -> int:
    """建表并幂等写入固定模拟租户的聚合数据，不导入行为明细。"""

    scenario = load_scenario()
    rows = build_demo_rows(scenario)
    async with engine.begin() as connection:
        await connection.execute(text(_CREATE_SCHEMA))
        await connection.execute(text(_CREATE_TABLE))
        await connection.execute(_UPSERT_ROWS, rows)
    return len(rows)


class PostgresReadOnlySqlWarehouseClient(SqlWarehouseClient):
    """用 Postgres 只读事务执行通过 SqlGuard 的查询。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def execute(self, query: SqlQuery) -> SqlQueryResult:
        if query.timeout_ms <= 0 or query.max_rows <= 0:
            raise HotNewsDataQualityError("invalid SQL query execution limits")
        started = perf_counter()
        try:
            async with self._engine.connect() as connection:
                async with connection.begin():
                    await connection.execute(text("SET TRANSACTION READ ONLY"))
                    await connection.execute(
                        text("SELECT set_config('statement_timeout', :timeout, true)"),
                        {"timeout": f"{query.timeout_ms}ms"},
                    )
                    result = await connection.execute(
                        text(query.sql), dict(query.params)
                    )
                    fetched = result.mappings().fetchmany(query.max_rows + 1)
            truncated = len(fetched) > query.max_rows
            rows = tuple(dict(row) for row in fetched[: query.max_rows])
        except Exception as exc:  # noqa: BLE001 - adapter returns a domain-level failure
            raise HotNewsDataQualityError(
                "read-only local SQL warehouse query failed"
            ) from exc
        elapsed_ms = max(0, int((perf_counter() - started) * 1000))
        return SqlQueryResult(
            rows=rows,
            elapsed_ms=elapsed_ms,
            truncated=truncated,
        )


@dataclass
class AuditedText2SqlMetricSource:
    """把 Text2SQL 批次证据保留下来，供本地演示结果摘要展示。"""

    source: Text2SqlNewsMetricSource
    last_batch: SqlMetricBatch | None = None

    async def fetch_snapshots(
        self,
        query: HotNewsMetricQuery,
    ) -> list[NewsMetricSnapshot]:
        self.last_batch = await self.source.fetch_batch(query)
        return list(self.last_batch.snapshots)


class EmptyDemoKnowledgeSearch:
    """本地链路不需要向企业知识库发起请求，关联新闻返回空集合。"""

    async def batch_search_related_news(
        self,
        queries: tuple[RelatedNewsSearchQuery, ...],
        *,
        tenant_id: str,
    ) -> dict[str, list[RelatedNews]]:
        del tenant_id
        return {query.query_id: [] for query in queries}


def _require_demo_settings(
    settings: Settings,
    *,
    require_hot_news: bool,
    require_text2sql: bool,
) -> None:
    database_host = urlsplit(str(settings.database_url)).hostname
    if database_host not in {
        "localhost",
        "127.0.0.1",
        "::1",
        "postgres",
        "writing-postgres",
        "host.docker.internal",
    }:
        raise SystemExit(
            "本地数仓模拟只允许连接 localhost 或本地 Docker Postgres；"
            "当前数据库地址未通过本地目标检查，未写入任何数据。"
        )

    required_apps = []
    if require_text2sql:
        required_apps.append(
            ("FASTGPT_TEXT2SQL_APP_ID", settings.fastgpt_text2sql_app_id)
        )
    if require_hot_news:
        required_apps.append(
            ("FASTGPT_HOT_NEWS_APP_ID", settings.fastgpt_hot_news_app_id)
        )
    missing = [
        name for name, value in required_apps if not value or not value.strip()
    ]
    if missing:
        raise SystemExit(
            "本演示要求配置 "
            + "、".join(missing)
            + "；不会静默跳过 Text2SQL 或热点分析模型调用。"
        )


async def run(*, seed_only: bool = False, template_only: bool = False) -> None:
    settings = get_settings()
    if seed_only and template_only:
        raise SystemExit("--seed-only 与 --template-only 不能同时使用")
    _require_demo_settings(
        settings,
        require_hot_news=not seed_only,
        require_text2sql=not seed_only and not template_only,
    )
    dependencies = build_hot_news_e2e_dependencies(
        tenant_id=DEMO_TENANT_ID,
        production_bundle_version=DEMO_BUNDLE_VERSION,
    )
    database = Database(settings)
    text2sql_client = (
        FastGPTClient(settings)
        if not seed_only and settings.fastgpt_text2sql_app_id
        else None
    )
    hot_news_runtime = None
    try:
        seeded_count = await seed_demo_warehouse(database.engine)
        print(
            json.dumps(
                {
                    "event": "demo_warehouse_ready",
                    "table": DEMO_TABLE,
                    "tenant_id": DEMO_TENANT_ID,
                    "seeded_aggregate_rows": seeded_count,
                    "source": "examples/data/hot_news_scenario.json",
                },
                ensure_ascii=False,
            )
        )
        if seed_only:
            return

        warehouse = PostgresReadOnlySqlWarehouseClient(database.engine)
        schema = build_text2sql_schema()
        if template_only:
            # Explicit local fallback when no Text2SQL App is configured.
            # It still exercises the same source, guard, and SQL executor.
            sql_source = create_text2sql_metric_source(
                settings,
                warehouse=warehouse,
                schema=schema,
                template=build_demo_metric_template(),
                fastgpt_client=text2sql_client,
            )
        else:
            # No template: the default command intentionally exercises the
            # actual FastGPT Text2SQL runner before the guard and SQL executor.
            sql_source = create_text2sql_metric_source(
                settings,
                warehouse=warehouse,
                schema=schema,
                template=None,
                fastgpt_client=text2sql_client,
            )
        metric_source = AuditedText2SqlMetricSource(source=sql_source)
        hot_news_runtime = create_hot_news_runtime(
            settings,
            baseline_provider=dependencies.baseline_provider,
            content_repository=dependencies.content_repository,
            policy=dependencies.policy,
            metric_source=metric_source,
            knowledge_search=EmptyDemoKnowledgeSearch(),
        )
        request = HotNewsRunRequest(
            tenant_id=DEMO_TENANT_ID,
            window_start=dependencies.window_start,
            window_end=dependencies.window_end,
            production_bundle_version=DEMO_BUNDLE_VERSION,
            workflow_version=DEMO_WORKFLOW_VERSION,
        )
        result = await hot_news_runtime.service.run(request)
        outcome = await PostgresHotNewsRunStore(database).save_completed(
            result=result
        )
        batch = metric_source.last_batch
        if batch is None:
            raise RuntimeError("Text2SQL source did not produce a query batch")

        print(
            json.dumps(
                {
                    "event": "hot_news_sql_demo_completed",
                    "run_id": outcome.run_id,
                    "query_source": batch.source,
                    "query_sha256": batch.sql_hash,
                    "text2sql_request_id": batch.request_id,
                    "snapshot_count": len(result.metric_snapshots),
                    "ranked_count": len(result.ranked_news),
                    "analyzed_count": len(result.analyzed_news),
                    "top_news": [
                        {
                            "rank": item.rank,
                            "news_id": item.current.news_id,
                            "title": (
                                dependencies.content_repository
                                .get_by_news_id(item.current.news_id)
                                .title
                            ),
                            "clicks": item.current.clicks,
                            "hot_score": str(item.hot_score.score),
                        }
                        for item in result.ranked_news[:5]
                    ],
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    finally:
        if hot_news_runtime is not None:
            await hot_news_runtime.close()
        if text2sql_client is not None:
            await text2sql_client.close()
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="热点 Agent 本地 SQL 数仓模拟")
    parser.add_argument(
        "--seed-only",
        action="store_true",
        help="只建模拟聚合表并写入样例，不调用 FastGPT Agent",
    )
    parser.add_argument(
        "--template-only",
        action="store_true",
        help="不需要 Text2SQL App，使用同一只读护栏执行确定性 SQL 模板",
    )
    args = parser.parse_args()
    asyncio.run(
        run(
            seed_only=args.seed_only,
            template_only=args.template_only,
        )
    )


if __name__ == "__main__":
    main()
