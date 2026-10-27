"""Real PG locks/migration and retained public-news safety replay; no model-quality claim."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import pytest_asyncio
from sqlalchemy import inspect, select, func
from sqlalchemy.exc import DBAPIError

from app.modules.knowledge.news_semantics import NewsSemanticService
from app.modules.knowledge.news_semantics_contracts import SemanticConfig, Features, Quote, blocks
from app.modules.knowledge.news_semantics_model import NewsSemanticModel
from app.modules.knowledge.news_semantics_store import NewsSemanticStore, metadata, analyses
from tests.test_news_vector_deletion import public_release, business, index, doc
from tests.test_news_semantics import TENANT, ScriptedInference, prepare_pair, features, understanding, add


def migration():
    path = Path(__file__).parents[1] / "alembic/versions/20261026_0024_news_semantics.py"
    spec = importlib.util.spec_from_file_location("semantic_revision_under_test", path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


@pytest_asyncio.fixture
async def semantic_business(business):
    b = business
    def create(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration().upgrade()
        for name, table in metadata.tables.items():
            actual = inspect(connection).get_columns(name)
            assert {c["name"]: c["nullable"] for c in actual} == {c.name: c.nullable for c in table.columns}
    if b.backend == "postgres":
        async with b.database.engine.begin() as connection:
            await connection.run_sync(create)
    else:
        metadata.create_all(b.database.engine)
    config = SemanticConfig(model_route="fixture", model_version="fixture-v1")
    store = NewsSemanticStore(b.database, b.ledger, config)
    inference = ScriptedInference()
    svc = NewsSemanticService(store=store, model=NewsSemanticModel(inference, config),
        lifecycle=b.service, query=b.query, config=config)
    b.service.semantics = b.query.semantics = svc
    return SimpleNamespace(**vars(b), semantics=svc, store=store, inference=inference,
                           semantic_config=config, raw_index=b.raw)


@pytest.mark.asyncio
async def test_concurrent_claims_and_retirements_have_one_winner(semantic_business):
    s = semantic_business
    if s.backend != "postgres":
        pytest.skip("advisory locks require PostgreSQL; SQLite is the sequential substitute")
    rows, pair = await prepare_pair(s)
    await s.store.retry(rows["a"])
    claimed = await asyncio.gather(*(s.store.claim(rows["a"]) for _ in range(8)))
    assert sum(c is not None for c in claimed) == 1
    jobs = await asyncio.gather(*(s.semantics.retire(tenant_id=TENANT, decision_id=pair["decision_id"])
                                  for _ in range(8)))
    assert len({j["job_id"] for j in jobs}) == 1
    await s.service.process_job(tenant_id=TENANT, job_id=jobs[0]["job_id"])


@pytest.mark.asyncio
async def test_target_withdrawal_between_vector_verify_and_sql_commit_keeps_source(semantic_business, monkeypatch):
    s = semantic_business
    _, pair = await prepare_pair(s)
    original, fired = s.service.index.verify_records, False
    async def verify(**kwargs):
        nonlocal fired
        result = await original(**kwargs)
        if not fired:
            fired = True
            await s.service.retract(tenant_id=TENANT, news_id="b", source_revision=2)
        return result
    monkeypatch.setattr(s.service.index, "verify_records", verify)
    with pytest.raises(ValueError, match="active"):
        await s.semantics.retire(tenant_id=TENANT, decision_id=pair["decision_id"])
    assert (await s.ledger.get_news(tenant_id=TENANT, news_id="a"))["status"] == "active"


@pytest.mark.asyncio
async def test_current_tencent_reports_cannot_retire_from_partial_extraction(semantic_business):
    s = semantic_business
    # Current frozen sample includes two different Tencent quarterly-report articles.
    originals = s.rows[1:3]
    assert all("腾讯" in r["title"] for r in originals)
    rows, jobs = [], []
    for i, source in enumerate(originals, start=1):
        await index(s, doc(s, row=i))
        row = await s.ledger.get_news(tenant_id=TENANT, news_id=source["news_id"])
        claim = await s.store.claim(row)
        snippet = blocks(row["body"])[0]
        f = features(snippet["text"])
        f.event.subject = "腾讯"
        f.event.occurrence_date = "2026-08-12"
        f.claims[0].subject = "腾讯"
        f.claims[0].value, f.claims[0].unit = "", ""
        # This intentionally incomplete scripted extraction is not a semantic gold label.
        # Even an overconfident model claiming completeness cannot erase remaining paragraphs.
        f.extraction_complete = True
        saved = await s.store.save_features(claim, row, f)
        rows.append(row)
        jobs.append(saved)
    pair = await s.store.save_pair(rows[0], rows[1], jobs[0], jobs[1], understanding())
    assert pair["result"]["redundant_news_id"] is None
    for r in rows:
        assert (await s.ledger.get_news(tenant_id=TENANT, news_id=r["news_id"]))["status"] == "active"


@pytest.mark.asyncio
async def test_migration_refuses_to_drop_semantic_audit_records(semantic_business):
    s = semantic_business
    if s.backend != "postgres":
        pytest.skip("PostgreSQL DO block")
    await s.semantics.process(await add(s, "a"))
    def downgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration().downgrade()
    with pytest.raises(DBAPIError, match="semantic audit data exists"):
        async with s.database.engine.begin() as connection:
            await connection.run_sync(downgrade)
    async with s.database.session() as session:
        assert (await session.execute(select(func.count()).select_from(analyses))).scalar_one() == 1
