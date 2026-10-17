"""Run the actual lifecycle Alembic revision against a disposable PG schema.

Requires NEWSAGENT_TIER_LIFECYCLE_TEST_DATABASE_URL. No existing application
tables or alembic_version rows are touched; the Operations context invokes this
revision's real upgrade/downgrade functions with a private search_path.
"""

from datetime import UTC, datetime
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import BigInteger, DateTime, create_engine, inspect, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.knowledge.tier_lifecycle_store import _jobs, _metadata, _news, _vectors


TABLE_NAMES = {"tier_lifecycle_news", "tier_lifecycle_jobs", "tier_lifecycle_vectors"}


def apply_revision(connection, callback):
    with Operations.context(MigrationContext.configure(connection)):
        callback()


@pytest.fixture
def migrated_schema():
    url = os.environ.get("NEWSAGENT_TIER_LIFECYCLE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set explicit PostgreSQL test URL to execute the real Alembic revision")
    path = Path(__file__).parents[1] / "alembic/versions/20261015_0023_tier_lifecycle.py"
    specification = importlib.util.spec_from_file_location("tier_lifecycle_migration_under_test", path)
    migration = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(migration)
    assert migration.revision == "20261015_0023"
    assert migration.down_revision == "20261010_0022"

    schema = "tier_migration_test_" + uuid4().hex
    admin = create_engine(url)
    engine, created = None, False
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        created = True
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT current_schema()")) == schema
            assert inspect(connection).get_table_names(schema=schema) == []
            apply_revision(connection, migration.upgrade)
        yield engine, migration, schema
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def news_row(*, tenant="tenant-a", news="news-one", document="document-one"):
    return dict(tenant_id=tenant, news_id=news, document_id=document,
                source_revision=1_760_000_000_000, generation=1, status="pending",
                event_hash="a" * 64, content_hash="b" * 64, title="迁移验证", body="测试正文",
                desired_tier="hot", current_job_id="1" * 32, accepted_job_id="1" * 32,
                config={}, updated_at=datetime.now(UTC))


def test_upgrade_has_real_types_runtime_columns_and_scoped_uniqueness(migrated_schema):
    engine, _, schema = migrated_schema
    with engine.begin() as connection:
        inspector = inspect(connection)
        assert set(inspector.get_table_names(schema=schema)) == TABLE_NAMES
        for name in TABLE_NAMES:
            columns = {column["name"]: column for column in inspector.get_columns(name, schema=schema)}
            runtime = _metadata.tables[name]
            assert set(columns) == set(runtime.columns.keys())
            assert {key: value["nullable"] for key, value in columns.items()} == {
                column.name: column.nullable for column in runtime.columns
            }
            for name in ("source_revision", "generation"):
                if name in columns:
                    assert isinstance(columns[name]["type"], BigInteger)
            for name in ("config", "payload", "records", "chunk_ids", "record"):
                if name in columns:
                    assert isinstance(columns[name]["type"], JSONB)
            for name in ("publish_time", "hold_until", "created_at", "updated_at"):
                if name in columns:
                    assert isinstance(columns[name]["type"], DateTime) and columns[name]["type"].timezone
        assert inspector.get_pk_constraint("tier_lifecycle_news", schema=schema)["constrained_columns"] == ["tenant_id", "news_id"]
        assert inspector.get_pk_constraint("tier_lifecycle_jobs", schema=schema)["constrained_columns"] == ["job_id"]
        assert inspector.get_pk_constraint("tier_lifecycle_vectors", schema=schema)["constrained_columns"] == ["chunk_id"]
        constraints = inspector.get_unique_constraints("tier_lifecycle_news", schema=schema)
        assert any(item["name"] == "uq_tier_lifecycle_news_document"
                   and item["column_names"] == ["tenant_id", "document_id"] for item in constraints)
        indexes = {item["name"]: item["column_names"]
                   for name in TABLE_NAMES for item in inspector.get_indexes(name, schema=schema)}
        assert indexes["ix_tier_lifecycle_jobs_news"] == ["tenant_id", "news_id"]
        assert indexes["ix_tier_lifecycle_jobs_pending"] == ["status", "job_id"]
        assert indexes["ix_tier_lifecycle_vectors_scope"] == ["tenant_id", "tier"]

        connection.execute(_news.insert().values(**news_row()))
        connection.execute(_news.insert().values(**news_row(tenant="tenant-b")))
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(_news.insert().values(**news_row(news="different-news")))
        assert connection.scalar(select(_news.c.source_revision).where(_news.c.tenant_id == "tenant-a")) == 1_760_000_000_000


def test_empty_downgrade_drops_only_private_lifecycle_tables(migrated_schema):
    engine, migration, schema = migrated_schema
    with engine.begin() as connection:
        # A sentinel in the private schema represents an unrelated existing table.
        connection.execute(text("CREATE TABLE unrelated_migration_sentinel (value integer NOT NULL)"))
        connection.execute(text("INSERT INTO unrelated_migration_sentinel VALUES (7)"))
        apply_revision(connection, migration.downgrade)
        assert inspect(connection).get_table_names(schema=schema) == ["unrelated_migration_sentinel"]
        assert connection.scalar(text("SELECT value FROM unrelated_migration_sentinel")) == 7


@pytest.mark.parametrize("table_name", sorted(TABLE_NAMES))
def test_nonempty_downgrade_refuses_without_losing_tables_or_records(migrated_schema, table_name):
    engine, migration, schema = migrated_schema
    now = datetime.now(UTC)
    if table_name == _news.name:
        statement = _news.insert().values(**news_row())
    elif table_name == _jobs.name:
        statement = _jobs.insert().values(job_id="1" * 32, tenant_id="tenant", news_id="news",
            source_revision=1, generation=1, kind="upsert", target_tier="hot", config={},
            payload={}, records=[], chunk_ids=[], phase="pending", status="pending",
            cleanup_dirty=True, attempts=0, created_at=now, updated_at=now)
    else:
        statement = _vectors.insert().values(chunk_id="1" * 64, tenant_id="tenant", tier="hot", record={})
    with engine.begin() as connection:
        connection.execute(statement)
    with pytest.raises(DBAPIError, match="downgrade would lose accepted work"):
        with engine.begin() as connection:
            apply_revision(connection, migration.downgrade)
    with engine.begin() as connection:
        assert set(inspect(connection).get_table_names(schema=schema)) == TABLE_NAMES
        assert connection.scalar(text(f'SELECT count(*) FROM "{table_name}"')) == 1
