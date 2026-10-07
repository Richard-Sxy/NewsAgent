"""连接和查询数仓。定义输惨结构契约，初始化本地演示数据，并通过 LocalPostgresWarehouseClient 在 PostgreSQL 中执行只读查询。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.clients.enterprise.sql_warehouse import SqlQuery, SqlQueryResult
from app.db.session import Database
from app.schemas.text2sql import Text2SqlColumn, Text2SqlSchema, Text2SqlTable
from app.sql_assistant.guard import SqlAssistantGuard
from app.sql_assistant.synthetic_profiles import (
    CLASSIC_PROFILE, ENTERPRISE_PROFILE, enterprise_fixture_rows,
    enterprise_manifest, fixture_fingerprint, validate_profile,
)
from app.sql_assistant.scaled_profiles import (
    DEFAULT_NEWS_COUNT, SCALED_PROFILE, ScaledTableHasher,
    iter_scaled_rows, scaled_manifest, scaled_news_ids, validate_news_count,
)
from app.sql_assistant.public_headlines import (
    PUBLIC_HEADLINES_PROFILE, public_catalog, public_manifest, public_news_ids,
    iter_public_rows, validate_public_news_count,
)

SCHEMA_VERSION = "news-warehouse-v1"
SCHEMA_SHA256 = "e4498800da447c72beb3be13ad375d9efa458605f8accf90af56b531c0aee3d0"
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "docs" / "sql-warehouse-schema-v1.md"
SCHEMA_VERSION_V2 = "news-warehouse-v2"
SCHEMA_SHA256_V2 = "b46023255d18b583048cbe8f5c5ce2a2e4372d3d867da2a1b24b493551c18273"
SCHEMA_PATH_V2 = SCHEMA_PATH.with_name("sql-warehouse-schema-v2.md")
SCHEMA_VERSION_V3 = "news-warehouse-v3"
SCHEMA_SHA256_V3 = "ac6982fcb42a3b9efe29db8c9a4524eebc51a2144ab8dbc78c69e91e458614b6"
SCHEMA_PATH_V3 = SCHEMA_PATH.with_name("sql-warehouse-schema-v3.md")
STREAMED_PROFILES = frozenset({SCALED_PROFILE, PUBLIC_HEADLINES_PROFILE})
DEMO_TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ISOLATION_TENANT_ID = UUID("33333333-3333-4333-8333-333333333333")
WINDOW_START = datetime(2026, 10, 3, tzinfo=timezone(timedelta(hours=8)))
WINDOW_END = WINDOW_START + timedelta(days=1)
ALLOWED_VIEW = "dw.news_behavior_aggregate"


class SqlWarehouseContractError(RuntimeError):
    """Frozen file or warehouse identity changed; never retry around it."""

    retryable = False


class SqlWarehouseTransientError(RuntimeError):
    """Temporary database connection, transaction or statement timeout failure."""

    retryable = True


@dataclass(frozen=True, slots=True)
class WarehouseContract:
    version: str
    sha256: str
    path: Path


def warehouse_contract(profile: str = CLASSIC_PROFILE) -> WarehouseContract:
    validate_profile(profile)
    if profile == PUBLIC_HEADLINES_PROFILE:
        return WarehouseContract(SCHEMA_VERSION_V3, SCHEMA_SHA256_V3, SCHEMA_PATH_V3)
    return (WarehouseContract(SCHEMA_VERSION_V2, SCHEMA_SHA256_V2, SCHEMA_PATH_V2)
            if profile == SCALED_PROFILE else WarehouseContract(SCHEMA_VERSION, SCHEMA_SHA256, SCHEMA_PATH))


def verify_schema_contract(profile: str = CLASSIC_PROFILE) -> str:
    """Reject changes and return the verified Markdown body for the UI."""

    try:
        contract = warehouse_contract(profile)
        contents = contract.path.read_bytes()
    except OSError as exc:
        raise SqlWarehouseContractError("frozen SQL warehouse contract is unavailable") from exc
    if hashlib.sha256(contents).hexdigest() != contract.sha256:
        raise SqlWarehouseContractError("frozen SQL warehouse contract SHA-256 mismatch")
    return contents.decode("utf-8")


# This tuple is the only database structure shared with the model.
_VIEW_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("tenant_id", "uuid", "后端身份上下文绑定的租户 ID"),
    ("news_id", "text", "租户内新闻标识"),
    ("event_time", "timestamptz", "小时桶起点；半开时间窗口过滤"),
    ("title", "text", "合成新闻标题"),
    ("content_type", "text", "article 图文或 video 视频"),
    ("category", "text", "栏目：科技、财经、体育、社会"),
    ("source", "text", "合成来源"),
    ("publish_time", "timestamptz", "新闻发布时间"),
    ("impressions", "bigint", "小时展示次数，可跨小时求和"),
    ("clicks", "bigint", "小时点击次数，可跨小时求和"),
    ("unique_users", "bigint", "仅小时内去重人数，禁止描述 SUM 为窗口去重人数"),
    ("total_duration_seconds", "numeric(20,2)", "小时消费时长总秒数"),
    ("effective_consumptions", "bigint", "小时有效消费次数"),
    ("interactions", "bigint", "小时互动次数"),
    ("baseline_impressions", "numeric(20,2)", "历史同小时展示次数参考均值"),
    ("baseline_clicks", "numeric(20,2)", "历史同小时点击次数参考均值"),
    ("baseline_effective_consumptions", "numeric(20,2)", "历史同小时有效消费参考均值"),
    ("baseline_interactions", "numeric(20,2)", "历史同小时互动次数参考均值"),
)

"""获取本地 SQL 仓库的 Text2SQL 模式，供模型推理使用。"""
def get_text2sql_schema(profile: str = CLASSIC_PROFILE) -> Text2SqlSchema:
    verify_schema_contract(profile)
    return Text2SqlSchema(
        dialect="postgres",
        tables=(
            Text2SqlTable(
                name=ALLOWED_VIEW,
                description=("公开真实标题引用与合成运营聚合；一行对应租户、新闻、小时桶"
                             if profile == PUBLIC_HEADLINES_PROFILE else "仅合成聚合数据；一行对应租户、新闻、小时桶"),
                columns=tuple(
                    Text2SqlColumn(name=name, data_type=data_type, description=(
                        {"title": "公开新闻原始标题", "source": "公开新闻原始来源"}.get(name, description)
                        if profile == PUBLIC_HEADLINES_PROFILE else description))
                    for name, data_type, description in _VIEW_COLUMNS
                ),
            ),
        ),
        max_rows=1000,
    )


def profile_news_count(profile: str, news_per_tenant: int) -> int:
    validate_profile(profile)
    if profile == PUBLIC_HEADLINES_PROFILE:
        return validate_public_news_count(news_per_tenant)
    return validate_news_count(news_per_tenant) if profile == SCALED_PROFILE else 12


def streamed_manifest(profile: str, news_per_tenant: int) -> dict[str, Any]:
    if profile == PUBLIC_HEADLINES_PROFILE:
        return public_manifest(news_per_tenant)
    if profile == SCALED_PROFILE:
        return scaled_manifest(news_per_tenant)
    raise ValueError("profile does not use streamed rows")


def demo_dataset_info(profile: str = CLASSIC_PROFILE, *, news_per_tenant: int = DEFAULT_NEWS_COUNT) -> dict[str, Any]:
    """Expected fixture shape for the primary demonstration tenant."""

    verify_schema_contract(profile)
    contract = warehouse_contract(profile)
    count = profile_news_count(profile, news_per_tenant)
    info = {
        "schema_version": contract.version,
        "schema_sha256": contract.sha256,
        "synthetic": True,
        "window_start": WINDOW_START.isoformat(),
        "window_end": WINDOW_END.isoformat(),
        "news_count": count,
        "metric_row_count": count * 24,
        "baseline_row_count": count * 24,
        "total_tenants": 2,
        "dataset_profile": profile,
    }
    if profile == ENTERPRISE_PROFILE:
        info.update(enterprise_manifest(_fixture_rows(profile)))
    if profile in STREAMED_PROFILES:
        manifest = streamed_manifest(profile, count)
        info.update({key: manifest[key] for key in ("dataset_version", "dataset_sha256", "enterprise_scenarios")})
        info.update(news_per_tenant=count, hours_per_news=24, total_news_count=count * 2,
                    total_metric_row_count=count * 48, total_baseline_row_count=count * 48)
    if profile == PUBLIC_HEADLINES_PROFILE:
        catalog = public_catalog()
        dates = [datetime.fromisoformat(article["published_at"]) for article in catalog["articles"]]
        info.update(headline_catalog_count=len(catalog["articles"]),
                    headline_catalog_sha256=manifest["catalog_sha256"],
                    headline_date_start=min(dates).date().isoformat(), headline_date_end=max(dates).date().isoformat(),
                    headline_source="qq-public-web", metric_data_synthetic=True)
    return info


def demo_news_ids(profile: str = CLASSIC_PROFILE, *, news_per_tenant: int = DEFAULT_NEWS_COUNT) -> tuple[str, ...]:
    validate_profile(profile)
    if profile == PUBLIC_HEADLINES_PROFILE:
        return public_news_ids(news_per_tenant)
    return scaled_news_ids(news_per_tenant) if profile == SCALED_PROFILE else tuple(f"demo-news-{index:03d}" for index in range(1, 13))


_DDL = (
    """CREATE TABLE IF NOT EXISTS dw.news_schema_contract (
        version text PRIMARY KEY,
        schema_sha256 text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS dw.dim_news (
        tenant_id uuid NOT NULL,
        news_id text NOT NULL,
        title text NOT NULL,
        content_type text NOT NULL CHECK (content_type IN ('article', 'video')),
        category text NOT NULL CHECK (category IN ('科技', '财经', '体育', '社会')),
        source text NOT NULL,
        publish_time timestamptz NOT NULL,
        PRIMARY KEY (tenant_id, news_id)
    )""",
    """CREATE TABLE IF NOT EXISTS dw.news_metric_hourly (
        tenant_id uuid NOT NULL,
        news_id text NOT NULL,
        event_time timestamptz NOT NULL,
        impressions bigint NOT NULL CHECK (impressions >= 0),
        clicks bigint NOT NULL CHECK (clicks >= 0 AND clicks <= impressions),
        unique_users bigint NOT NULL CHECK (unique_users >= 0 AND unique_users <= clicks),
        total_duration_seconds numeric(20,2) NOT NULL CHECK (total_duration_seconds >= 0),
        effective_consumptions bigint NOT NULL CHECK (
            effective_consumptions >= 0 AND effective_consumptions <= clicks
        ),
        interactions bigint NOT NULL CHECK (interactions >= 0),
        PRIMARY KEY (tenant_id, news_id, event_time),
        FOREIGN KEY (tenant_id, news_id) REFERENCES dw.dim_news(tenant_id, news_id)
    )""",
    """CREATE TABLE IF NOT EXISTS dw.news_metric_baseline_hourly (
        tenant_id uuid NOT NULL,
        news_id text NOT NULL,
        event_time timestamptz NOT NULL,
        baseline_impressions numeric(20,2) NOT NULL CHECK (baseline_impressions >= 0),
        baseline_clicks numeric(20,2) NOT NULL CHECK (baseline_clicks >= 0),
        baseline_effective_consumptions numeric(20,2) NOT NULL CHECK (
            baseline_effective_consumptions >= 0
        ),
        baseline_interactions numeric(20,2) NOT NULL CHECK (baseline_interactions >= 0),
        PRIMARY KEY (tenant_id, news_id, event_time),
        FOREIGN KEY (tenant_id, news_id, event_time)
            REFERENCES dw.news_metric_hourly(tenant_id, news_id, event_time)
    )""",
)
_VIEW_DDL = """CREATE VIEW dw.news_behavior_aggregate AS
    SELECT m.tenant_id, m.news_id, m.event_time,
        n.title, n.content_type, n.category, n.source, n.publish_time,
        m.impressions, m.clicks, m.unique_users,
        m.total_duration_seconds, m.effective_consumptions, m.interactions,
        b.baseline_impressions, b.baseline_clicks,
        b.baseline_effective_consumptions, b.baseline_interactions
    FROM dw.news_metric_hourly AS m
    JOIN dw.dim_news AS n
        ON n.tenant_id = m.tenant_id AND n.news_id = m.news_id
    JOIN dw.news_metric_baseline_hourly AS b
        ON b.tenant_id = m.tenant_id
        AND b.news_id = m.news_id
        AND b.event_time = m.event_time
"""

_NEWS = (
    ("城市智能交通试点发布阶段进展", "article", "科技"),
    ("国产机器人展示精细操作能力", "video", "科技"),
    ("研究团队公布低碳电池材料成果", "article", "科技"),
    ("假日消费市场呈现多样化趋势", "article", "财经"),
    ("中小企业服务平台上线新工具", "video", "财经"),
    ("区域物流枢纽完成扩容", "article", "财经"),
    ("城市马拉松公布赛事安排", "video", "体育"),
    ("青年球队完成友谊赛", "article", "体育"),
    ("社区运动场开放夜间时段", "video", "体育"),
    ("公共图书馆推出便民阅读服务", "article", "社会"),
    ("志愿团队开展安全知识演示", "video", "社会"),
    ("老旧社区完成无障碍设施改造", "article", "社会"),
)


def _fixture_rows(dataset_profile: str = CLASSIC_PROFILE) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    validate_profile(dataset_profile)
    if dataset_profile in STREAMED_PROFILES:
        raise ValueError("scaled profile rows must be streamed")
    news: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    baselines: list[dict[str, Any]] = []
    for tenant_index, tenant_id in enumerate((DEMO_TENANT_ID, ISOLATION_TENANT_ID)):
        for index, (title, content_type, category) in enumerate(_NEWS, start=1):
            news_id = f"demo-news-{index:03d}"
            news.append({
                "tenant_id": tenant_id,
                "news_id": news_id,
                "title": f"[合成样本] {title}" if tenant_index == 0 else f"[隔离样本] {title}",
                "content_type": content_type,
                "category": category,
                "source": "本地模拟新闻中心",
                "publish_time": WINDOW_START - timedelta(hours=12 - index),
            })
            for hour in range(24):
                # Fixed inputs make rankings reproducible, with genuine hour-to-hour variation.
                impressions = 700 + index * 130 + hour * 17 + (hour % 5) * 23
                if tenant_index:
                    impressions *= 8  # A visible isolation canary, never returned to the main tenant.
                clicks = impressions * (9 + index % 7 + hour % 4) // 100
                effective = clicks * (57 + index % 8) // 100
                interactions = clicks * (10 + index % 6) // 100
                common = {"tenant_id": tenant_id, "news_id": news_id,
                          "event_time": WINDOW_START + timedelta(hours=hour)}
                metrics.append({
                    **common,
                    "impressions": impressions,
                    "clicks": clicks,
                    "unique_users": clicks * 83 // 100,
                    "total_duration_seconds": Decimal(clicks * (30 + index * 3)),
                    "effective_consumptions": effective,
                    "interactions": interactions,
                })
                divisor = Decimal("1.15") if index % 3 else Decimal("0.85")
                baselines.append({
                    **common,
                    "baseline_impressions": (Decimal(impressions) / divisor).quantize(Decimal("0.01")),
                    "baseline_clicks": (Decimal(clicks) / divisor).quantize(Decimal("0.01")),
                    "baseline_effective_consumptions": (Decimal(effective) / divisor).quantize(Decimal("0.01")),
                    "baseline_interactions": (Decimal(interactions) / divisor).quantize(Decimal("0.01")),
                })
    rows = (news, metrics, baselines)
    return enterprise_fixture_rows(rows) if dataset_profile == ENTERPRISE_PROFILE else rows


_FIXTURE_FIELDS = (
    ("dim_news", "tenant_id, news_id, title, content_type, category, source, publish_time"),
    ("news_metric_hourly", "tenant_id, news_id, event_time, impressions, clicks, unique_users, "
     "total_duration_seconds, effective_consumptions, interactions"),
    ("news_metric_baseline_hourly", "tenant_id, news_id, event_time, baseline_impressions, "
     "baseline_clicks, baseline_effective_consumptions, baseline_interactions"),
)


async def _stored_fixture_rows(connection) -> tuple[list[dict], list[dict], list[dict]]:
    # Fixed owner-side queries include all tenants and rows to detect additions,
    # missing buckets and value drift. This Port is never available to the model.
    tables = []
    for table, fields in _FIXTURE_FIELDS:
        result = await connection.execute(text(f"SELECT {fields} FROM dw.{table}"))
        tables.append([dict(row) for row in result.mappings().all()])
    return tuple(tables)


async def _profile_contract(connection, dataset_profile: str, expected: dict | None) -> bool:
    """Check ownership before seeding. Return whether enterprise is already owned."""
    ledger = await connection.scalar(text("SELECT to_regclass('local_simulation.synthetic_dataset_contract')"))
    if ledger is not None:
        rows = (await connection.execute(text(
            "SELECT dataset_profile, dataset_version, dataset_sha256 "
            "FROM local_simulation.synthetic_dataset_contract"
        ))).mappings().all()
        if (expected is None or len(rows) != 1
                or any(rows[0][key] != expected[key] for key in
                       ("dataset_profile", "dataset_version", "dataset_sha256"))):
            raise SqlWarehouseContractError("synthetic dataset profile/version/hash mismatch; use a new isolated warehouse")
        return True
    if dataset_profile in {ENTERPRISE_PROFILE, *STREAMED_PROFILES}:
        for table, _fields in _FIXTURE_FIELDS:
            present = await connection.scalar(text(f"SELECT to_regclass('dw.{table}')"))
            if present is not None and await connection.scalar(text(f"SELECT EXISTS (SELECT 1 FROM dw.{table})")):
                raise SqlWarehouseContractError("enterprise profile requires a new empty isolated warehouse")
    return False


async def _verify_profile_rows(connection, expected_rows) -> None:
    actual = await _stored_fixture_rows(connection)
    if fixture_fingerprint(actual) != fixture_fingerprint(expected_rows):
        raise SqlWarehouseContractError("synthetic dataset rows are missing or changed; refuse to repair existing data")


def _scaled_batches(table: str, news_per_tenant: int, *, batch_size: int = 500,
                    dataset_profile: str = SCALED_PROFILE):
    if type(batch_size) is not int or not 1 <= batch_size <= 500:
        raise ValueError("scaled fixture batch must contain 1 to 500 rows")
    batch = []
    iterator = iter_public_rows if dataset_profile == PUBLIC_HEADLINES_PROFILE else iter_scaled_rows
    if dataset_profile not in STREAMED_PROFILES:
        raise ValueError("profile does not use streamed rows")
    for row in iterator(table, news_per_tenant):
        batch.append(row)
        if len(batch) == batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


async def _verify_scaled_rows(connection, expected: dict) -> None:
    for table, fields in _FIXTURE_FIELDS:
        order = "tenant_id, news_id" + (", event_time" if table != "dim_news" else "")
        cursor = await connection.stream(text(f"SELECT {fields} FROM dw.{table} ORDER BY {order}"))
        hasher = ScaledTableHasher(table)
        try:
            async for partition in cursor.mappings().partitions(500):
                for row in partition:
                    hasher.update(row)
        finally:
            await cursor.close()
        approved = expected["tables"][table]
        if hasher.count != approved["rows"] or hasher.digest() != approved["sha256"]:
            raise SqlWarehouseContractError("scaled synthetic dataset rows are missing or changed; refuse to repair existing data")


async def _ensure_scaled_schema(connection, dataset_profile: str = SCALED_PROFILE) -> None:
    contract = warehouse_contract(dataset_profile)
    await connection.execute(text("CREATE SCHEMA IF NOT EXISTS dw"))
    exists = await connection.scalar(text("SELECT to_regclass('dw.news_schema_contract')"))
    if exists is None:
        occupied = await connection.scalar(text("""SELECT EXISTS (
            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'dw' AND c.relname IN (
                'dim_news', 'news_metric_hourly', 'news_metric_baseline_hourly', 'news_behavior_aggregate'
            ))"""))
        if occupied:
            raise SqlWarehouseContractError("existing dw objects have no versioned ownership contract")
    else:
        rows = (await connection.execute(text("SELECT version, schema_sha256 FROM dw.news_schema_contract"))).mappings().all()
        if len(rows) != 1 or rows[0]["version"] != contract.version or rows[0]["schema_sha256"] != contract.sha256:
            raise SqlWarehouseContractError("database SQL warehouse contract version/hash mismatch")
    for ddl in _DDL:
        await connection.execute(text(ddl))
    await connection.execute(text("""INSERT INTO dw.news_schema_contract(version, schema_sha256)
        VALUES (:version, :schema_sha256) ON CONFLICT DO NOTHING"""),
        {"version": contract.version, "schema_sha256": contract.sha256})
    if await connection.scalar(text("SELECT to_regclass('dw.news_behavior_aggregate')")) is None:
        await connection.execute(text(_VIEW_DDL))
    columns = (await connection.execute(text("""SELECT column_name FROM information_schema.columns
        WHERE table_schema='dw' AND table_name='news_behavior_aggregate' ORDER BY ordinal_position"""))).scalars().all()
    if tuple(columns) != tuple(column[0] for column in _VIEW_COLUMNS):
        raise SqlWarehouseContractError("database SQL warehouse view columns differ from its contract")
    for table, name in (("news_metric_hourly", "news_metric_hourly_tenant_hour_news_idx"),
                        ("news_metric_baseline_hourly", "news_metric_baseline_hourly_tenant_hour_news_idx")):
        await connection.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON dw.{table} (tenant_id, event_time, news_id)"))


async def _initialize_scaled_warehouse(database: Database, *, news_per_tenant: int,
                                       dataset_profile: str = SCALED_PROFILE) -> dict[str, Any]:
    expected = streamed_manifest(dataset_profile, news_per_tenant)
    async with database.engine.begin() as connection:
        await connection.execute(text("SELECT pg_advisory_xact_lock(684917302613)"))
        owned = await _profile_contract(connection, dataset_profile, expected)
        await _ensure_scaled_schema(connection, dataset_profile)
        if not owned:
            for table, fields in _FIXTURE_FIELDS:
                names = [field.strip() for field in fields.split(",")]
                parameters = ", ".join(f":{field}" for field in names)
                statement = text(f"INSERT INTO dw.{table} ({fields}) VALUES ({parameters}) ON CONFLICT DO NOTHING")
                for batch in _scaled_batches(table, news_per_tenant, dataset_profile=dataset_profile):
                    await connection.execute(statement, batch)
        # Read every real row in the same atomic transaction. Never cache the
        # actual database digest and never refill an already-owned missing row.
        await _verify_scaled_rows(connection, expected)
        if not owned:
            await connection.execute(text("CREATE SCHEMA IF NOT EXISTS local_simulation"))
            await connection.execute(text("""CREATE TABLE local_simulation.synthetic_dataset_contract (
                dataset_profile text PRIMARY KEY, dataset_version text NOT NULL,
                dataset_sha256 text NOT NULL, created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""))
            await connection.execute(text("""INSERT INTO local_simulation.synthetic_dataset_contract
                (dataset_profile, dataset_version, dataset_sha256)
                VALUES (:dataset_profile, :dataset_version, :dataset_sha256)"""),
                {key: expected[key] for key in ("dataset_profile", "dataset_version", "dataset_sha256")})
    return demo_dataset_info(dataset_profile, news_per_tenant=news_per_tenant)


async def initialize_demo_warehouse(database: Database, *, environment: str = "e2e",
                                    dataset_profile: str = CLASSIC_PROFILE,
                                    news_per_tenant: int = DEFAULT_NEWS_COUNT) -> dict[str, Any]:
    """Create and append fixtures only inside the explicitly isolated E2E environment."""

    verify_schema_contract(dataset_profile)
    validate_profile(dataset_profile)
    if environment != "e2e":
        raise SqlWarehouseContractError("local SQL warehouse initialization requires environment=e2e")
    if database.engine.dialect.name != "postgresql":
        raise SqlWarehouseContractError("local SQL warehouse requires PostgreSQL")
    if dataset_profile in STREAMED_PROFILES:
        return await _initialize_scaled_warehouse(database,
            news_per_tenant=profile_news_count(dataset_profile, news_per_tenant), dataset_profile=dataset_profile)
    news, metrics, baselines = _fixture_rows(dataset_profile)
    expected = enterprise_manifest((news, metrics, baselines)) if dataset_profile == ENTERPRISE_PROFILE else None
    async with database.engine.begin() as connection:
        await connection.execute(text("SELECT pg_advisory_xact_lock(684917302613)"))
        profile_owned = await _profile_contract(connection, dataset_profile, expected)
        await connection.execute(text("CREATE SCHEMA IF NOT EXISTS dw"))
        contract_exists = await connection.scalar(text("SELECT to_regclass('dw.news_schema_contract')"))
        if contract_exists is None:
            occupied = await connection.scalar(text("""SELECT EXISTS (
                SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'dw' AND c.relname IN (
                    'dim_news', 'news_metric_hourly', 'news_metric_baseline_hourly',
                    'news_behavior_aggregate'
                )
            )"""))
            if occupied:
                raise SqlWarehouseContractError("existing dw objects have no v1 ownership contract")
        else:
            rows = (await connection.execute(text(
                "SELECT version, schema_sha256 FROM dw.news_schema_contract"
            ))).mappings().all()
            if len(rows) != 1 or rows[0]["version"] != SCHEMA_VERSION or rows[0]["schema_sha256"] != SCHEMA_SHA256:
                raise SqlWarehouseContractError("database SQL warehouse contract version/hash mismatch")
        for ddl in _DDL:
            await connection.execute(text(ddl))
        await connection.execute(text("""INSERT INTO dw.news_schema_contract(version, schema_sha256)
            VALUES (:version, :schema_sha256) ON CONFLICT DO NOTHING"""),
            {"version": SCHEMA_VERSION, "schema_sha256": SCHEMA_SHA256})
        if await connection.scalar(text("SELECT to_regclass('dw.news_behavior_aggregate')")) is None:
            await connection.execute(text(_VIEW_DDL))
        actual_columns = (await connection.execute(text("""SELECT column_name
            FROM information_schema.columns WHERE table_schema='dw'
            AND table_name='news_behavior_aggregate' ORDER BY ordinal_position"""))).scalars().all()
        if tuple(actual_columns) != tuple(column[0] for column in _VIEW_COLUMNS):
            raise SqlWarehouseContractError("database SQL warehouse view columns differ from v1")
        if profile_owned:
            # Never let ON CONFLICT mask drift or silently refill a missing row.
            await _verify_profile_rows(connection, (news, metrics, baselines))
        await connection.execute(text("""INSERT INTO dw.dim_news
            (tenant_id, news_id, title, content_type, category, source, publish_time)
            VALUES (:tenant_id, :news_id, :title, :content_type, :category, :source, :publish_time)
            ON CONFLICT DO NOTHING"""), news)
        await connection.execute(text("""INSERT INTO dw.news_metric_hourly
            (tenant_id, news_id, event_time, impressions, clicks, unique_users,
             total_duration_seconds, effective_consumptions, interactions)
            VALUES (:tenant_id, :news_id, :event_time, :impressions, :clicks, :unique_users,
                    :total_duration_seconds, :effective_consumptions, :interactions)
            ON CONFLICT DO NOTHING"""), metrics)
        await connection.execute(text("""INSERT INTO dw.news_metric_baseline_hourly
            (tenant_id, news_id, event_time, baseline_impressions, baseline_clicks,
             baseline_effective_consumptions, baseline_interactions)
            VALUES (:tenant_id, :news_id, :event_time, :baseline_impressions, :baseline_clicks,
                    :baseline_effective_consumptions, :baseline_interactions)
            ON CONFLICT DO NOTHING"""), baselines)
        if dataset_profile == ENTERPRISE_PROFILE and not profile_owned:
            await _verify_profile_rows(connection, (news, metrics, baselines))
            await connection.execute(text("CREATE SCHEMA IF NOT EXISTS local_simulation"))
            await connection.execute(text("""CREATE TABLE local_simulation.synthetic_dataset_contract (
                dataset_profile text PRIMARY KEY,
                dataset_version text NOT NULL,
                dataset_sha256 text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""))
            await connection.execute(text("""INSERT INTO local_simulation.synthetic_dataset_contract
                (dataset_profile, dataset_version, dataset_sha256)
                VALUES (:dataset_profile, :dataset_version, :dataset_sha256)"""),
                {key: expected[key] for key in ("dataset_profile", "dataset_version", "dataset_sha256")})
        counts = (await connection.execute(text("""SELECT
            (SELECT COUNT(*) FROM dw.dim_news WHERE tenant_id=:tenant_id) AS news_count,
            (SELECT COUNT(*) FROM dw.news_metric_hourly WHERE tenant_id=:tenant_id
             AND event_time>=:window_start AND event_time<:window_end) AS metric_row_count,
            (SELECT COUNT(*) FROM dw.news_metric_baseline_hourly WHERE tenant_id=:tenant_id
             AND event_time>=:window_start AND event_time<:window_end) AS baseline_row_count
        """), {"tenant_id": DEMO_TENANT_ID, "window_start": WINDOW_START,
                 "window_end": WINDOW_END})).mappings().one()
    return {**demo_dataset_info(dataset_profile), **dict(counts)}


class LocalPostgresSqlWarehouseClient:
    """Execute guarded SQL in a real read-only transaction with bound parameters."""

    def __init__(self, database: Database, *, tenant_id: str,
                 dataset_profile: str = CLASSIC_PROFILE, news_per_tenant: int = DEFAULT_NEWS_COUNT) -> None:
        verify_schema_contract(dataset_profile)
        if dataset_profile in STREAMED_PROFILES:
            profile_news_count(dataset_profile, news_per_tenant)
        self._dataset_profile = dataset_profile
        self._database = database
        self._tenant_id = UUID(str(tenant_id))

    async def execute(self, query: SqlQuery) -> SqlQueryResult:
        verify_schema_contract(self._dataset_profile)
        try:
            bound_tenant = UUID(str(query.params.get("tenant_id", "")))
        except (TypeError, ValueError) as exc:
            raise SqlWarehouseContractError("SQL query is missing a valid bound tenant") from exc
        if bound_tenant != self._tenant_id:
            raise SqlWarehouseContractError("SQL query tenant differs from authenticated tenant")
        if not 1 <= query.max_rows <= 1000 or not 1 <= query.timeout_ms <= 30000:
            raise SqlWarehouseContractError("SQL query exceeds local row or timeout limits")
        row_limit = query.params.get("row_limit")
        if isinstance(row_limit, bool) or not isinstance(row_limit, int) or not 1 <= row_limit <= query.max_rows:
            raise SqlWarehouseContractError("SQL query row_limit exceeds its execution budget")
        parameters = dict(query.params)
        # Defense in depth: the executor independently applies the same full AST,
        # metric-formula and scope gate as preview, even for an internal caller.
        parameters["tenant_id"] = str(bound_tenant)
        SqlAssistantGuard(max_rows=query.max_rows).validate(query.sql, parameters)
        parameters["tenant_id"] = bound_tenant
        started = monotonic()
        try:
            async with self._database.engine.connect() as connection:
                async with connection.begin():
                    await connection.execute(text("SET TRANSACTION READ ONLY"))
                    await connection.execute(text(
                        "SELECT set_config('statement_timeout', :statement_timeout, true)"
                    ), {"statement_timeout": str(query.timeout_ms)})
                    result = await connection.execute(text(query.sql), parameters)
                    rows = result.mappings().fetchmany(query.max_rows + 1)
                    # No table-write statement is issued anywhere on this query connection.
            return SqlQueryResult(
                rows=tuple(dict(row) for row in rows[:query.max_rows]),
                elapsed_ms=max(1, int((monotonic() - started) * 1000)),
                truncated=len(rows) > query.max_rows,
            )
        except DBAPIError as exc:
            sqlstate = getattr(exc.orig, "sqlstate", "") or ""
            if exc.connection_invalidated or sqlstate.startswith("08") or sqlstate in {"40001", "40P01", "57014", "57P01", "57P02", "57P03"}:
                raise SqlWarehouseTransientError("local SQL warehouse is temporarily unavailable or timed out") from exc
            raise SqlWarehouseContractError("local SQL warehouse rejected the guarded query") from exc
        except SQLAlchemyError as exc:
            raise SqlWarehouseTransientError("local SQL warehouse connection failed") from exc
