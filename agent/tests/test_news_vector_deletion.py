"""Business replay: frozen public news -> SQL ledger -> vector deletion -> recall.

Run with NEWSAGENT_NEWS_FIXTURE_DIR pointing at a verified unified content
release. Positive duplicate IDs are explicitly constructed test copies; real
source text is read-only and is never checked into fixtures. PostgreSQL tests
use a private schema; the vector backend here is the persistent local SQL Port,
not enterprise Milvus or model-quality acceptance.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
import httpx
from pydantic import SecretStr
import pytest
import pytest_asyncio
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.api.knowledge import router
from app.analytics.lifecycle_news_content import LifecycleNewsContentRepository
from app.modules.knowledge.document import KnowledgeDocument
from app.modules.knowledge.duplicate_policy import duplicate_evidence, normalized_text
from app.modules.knowledge.tier_lifecycle import KnowledgeLifecycleService, record_from_dict
from app.modules.knowledge.tier_lifecycle_config import load_knowledge_lifecycle_config
from app.modules.knowledge.tier_lifecycle_index import GuardedTieredIndex
from app.modules.knowledge.tier_lifecycle_local_index import LocalLifecycleVectorIndex
from app.modules.knowledge.tier_lifecycle_query import LifecycleKnowledgeSearch
from app.modules.knowledge.tier_lifecycle_store import TierLifecycleStore, _metadata, _jobs, _vectors
from app.model_runtime.local_embedding import LocalHashEmbedding
from app.model_runtime.query_embedding import ModelQueryEmbedder
from app.retrieval.tiered_vector import VectorTier


TENANT = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


@pytest.fixture(scope="session")
def public_release():
    configured = os.environ.get("NEWSAGENT_NEWS_FIXTURE_DIR")
    if not configured:
        pytest.skip("set explicit frozen public-news package for business replay")
    root = Path(configured)
    raw_manifest = (root / "manifest.json").read_bytes()
    manifest = json.loads(raw_manifest)
    # Only fixed package filenames are read, regardless of manifest contents.
    for name in ("versions.jsonl", "release_items.jsonl", "provenance.jsonl"):
        assert sha256((root / name).read_bytes()).hexdigest() == manifest["files"][name]["sha256"]
    with (root / "release_items.jsonl").open(encoding="utf-8") as stream:
        selected = {json.loads(line)["record_sha256"] for line in stream}
    with (root / "versions.jsonl").open(encoding="utf-8") as stream:
        rows = [row for row in map(json.loads, stream) if row["record_sha256"] in selected]
    bodies = [row for row in rows if row.get("body")]
    groups = {}
    for row in bodies:
        if len(row["body"]) >= 100:
            groups.setdefault(sha256(row["body"].encode()).hexdigest(), []).append(row["news_id"])
    eligible = [row for row in bodies if row.get("title") and row.get("publish_time")
                and 100 <= len(normalized_text(row["body"])) <= 4000
                and row["content_type"] == "article"]
    eligible.sort(key=lambda row: row["news_id"])
    assert len(eligible) >= 3
    report = {"schema_version": 1, "source_release": manifest["release"],
        "manifest_sha256": sha256(raw_manifest).hexdigest(),
        "selected_count": len(rows), "body_count": len(bodies),
        "exact_body_duplicate_groups_min_100_chars": sum(len(v) > 1 for v in groups.values()),
        "sample_sources": [{k: row.get(k) for k in
            ("news_id", "title", "record_sha256", "body_sha256", "publish_time")} for row in eligible[:3]],
        "positive_cases": "derived test copies; not observed duplicates in the public corpus",
        "embedding": "LocalHashEmbedding; not semantic-quality acceptance",
        "vector_backend": "persistent_local_sql; not Milvus acceptance",
        "execution": "in-process authenticated HTTP and Worker service handlers; no Temporal server",
        "distributed_otel_export": "not enabled in this isolated test harness",
        "cases": []}
    yield eligible[:3], report
    # Prove that replay did not mutate source files.
    assert (root / "manifest.json").read_bytes() == raw_manifest
    for name in ("versions.jsonl", "release_items.jsonl", "provenance.jsonl"):
        assert sha256((root / name).read_bytes()).hexdigest() == manifest["files"][name]["sha256"]
    report["source_unchanged"] = True
    output = os.environ.get("NEWSAGENT_VECTOR_TEST_REPORT")
    if output:
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def business(request, tmp_path, public_release):
    rows, report = public_release
    backend = request.param
    admin = None
    if backend == "postgres":
        url = os.environ.get("NEWSAGENT_TIER_LIFECYCLE_TEST_DATABASE_URL")
        if not url:
            pytest.skip("set explicit disposable PostgreSQL test URL")
        schema = "news_delete_test_" + uuid4().hex
        admin = create_async_engine(url)
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(_metadata.create_all)

        @asynccontextmanager
        async def session():
            async with factory.begin() as current:
                yield current
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'business.sqlite'}")
        _metadata.create_all(engine)

        @asynccontextmanager
        async def session():
            with Session(engine) as current:
                async def execute(*args, **kwargs):
                    return current.execute(*args, **kwargs)
                try:
                    yield SimpleNamespace(execute=execute)
                    current.commit()
                except BaseException:
                    current.rollback()
                    raise

    database = SimpleNamespace(engine=engine, session=session)
    config = load_knowledge_lifecycle_config(DEPLOY / "knowledge-lifecycle.local.example.yml")
    ledger = TierLifecycleStore(database=database)
    raw = LocalLifecycleVectorIndex(database=database, max_search_rows=50000, environment="e2e")
    guarded = GuardedTieredIndex(raw, ledger, max_candidates=config.query_max_candidates)
    embedding = LocalHashEmbedding(model_routes=(config.embedding_version,),
                                   dimensions=config.expected_embedding_dimensions)

    def reopen(index=None):
        return KnowledgeLifecycleService(ledger=TierLifecycleStore(database=database),
            index=index or guarded, embedding=embedding, config=config)

    service = reopen()
    query = LifecycleKnowledgeSearch(index=guarded, ledger=ledger, config=config,
        embedder=ModelQueryEmbedder(embedding, version=config.embedding_version))
    app = FastAPI()
    app.state.settings = SimpleNamespace(knowledge_ingest_token=SecretStr("isolated-test-token"))
    app.state.knowledge_store = service
    app.state.knowledge_lifecycle = SimpleNamespace(service=service, query=query)
    app.include_router(router)
    now = max(datetime.fromisoformat(row["publish_time"]) for row in rows) + timedelta(seconds=1)
    value = SimpleNamespace(rows=rows, database=database, ledger=ledger, raw=raw, guarded=guarded,
        service=service, query=query, reopen=reopen, config=config, app=app, now=now, backend=backend,
        content=LifecycleNewsContentRepository(database=database))
    try:
        yield value
    finally:
        async with database.session() as session:
            jobs = (await session.execute(select(_jobs.c.job_id, _jobs.c.news_id, _jobs.c.kind,
                _jobs.c.status, _jobs.c.phase, _jobs.c.cleanup_dirty, _jobs.c.attempts,
                _jobs.c.last_error))).mappings().all()
            count = (await session.execute(select(_vectors.c.chunk_id))).scalars().all()
        report["cases"].append({"test": request.node.name, "ledger_backend": backend,
            "jobs": [dict(row) for row in jobs], "remaining_vector_rows": len(count)})
        if admin:
            await engine.dispose()
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()
        else:
            engine.dispose()


def doc(case, *, copy=False, row=0, revision=1, body=None, title=None, published=None):
    source = case.rows[row]
    news_id = source["news_id"] + ("-test-copy" if copy else "")
    return KnowledgeDocument(document_id=news_id, title=title or source["title"],
        text=source["body"] if body is None else body, media_type="article", text_source="public-release-test",
        source_url=source.get("url") or "", metadata={"news_id": news_id,
            "content_version": str(revision), "publish_time": published or source["publish_time"]})


async def index(case, document, *, tenant=TENANT):
    job = await case.service.accept(tenant_id=tenant, document=document, now=case.now)
    await case.service.process_job(tenant_id=tenant, job_id=job["job_id"])
    return await case.ledger.get_job(tenant_id=tenant, job_id=job["job_id"])


def request_for(source, replacement):
    return dict(tenant_id=source["tenant_id"], news_id=source["news_id"],
        source_revision=source["source_revision"], source_job_id=source["job_id"],
        replacement_news_id=replacement["news_id"], replacement_revision=replacement["source_revision"],
        replacement_job_id=replacement["job_id"])


async def found(case):
    result = await case.query.search(query_text=case.rows[0]["title"], tenant_id=TENANT, limit=10)
    return {hit.news_id for hit in result["hits"]}


async def assert_deleted(case, job):
    assert not await case.raw.contains(tier=VectorTier(job["target_tier"]), chunk_ids=job["chunk_ids"])


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", list(VectorTier))
async def test_duplicate_http_retirement_deletes_only_manifest_and_keeps_source_content(business, tier):
    case = business
    source = await index(case, doc(case))
    if source["target_tier"] != tier.value:
        source = await case.ledger.request_migration(tenant_id=TENANT, news_id=source["news_id"], target_tier=tier.value)
        await case.service.process_job(tenant_id=TENANT, job_id=source["job_id"])
        source = await case.ledger.get_job(tenant_id=TENANT, job_id=source["job_id"])
    replacement = await index(case, doc(case, copy=True))
    unrelated = await index(case, doc(case, row=1))
    other_tenant = await index(case, doc(case), tenant=OTHER)
    ids = {source["news_id"], replacement["news_id"], unrelated["news_id"]}
    assert await found(case) == ids
    payload = request_for(source, replacement)
    payload.pop("tenant_id")
    payload.pop("news_id")
    endpoint = f'/api/v1/knowledge/news/{source["news_id"]}/retire-duplicate'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=case.app), base_url="http://test") as client:
        assert (await client.post(endpoint, json=payload)).status_code in (401, 422)
        headers = {"Authorization": "Bearer isolated-test-token", "X-Tenant-ID": TENANT}
        response = await client.post(endpoint, json=payload, headers=headers)
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        assert (await client.post(endpoint, json=payload, headers=headers)).json()["job_id"] == job_id
        assert "body" not in response.text and "records" not in response.text
        assert await found(case) == ids - {source["news_id"]}
        assert await case.raw.contains(tier=tier, chunk_ids=source["chunk_ids"])
        await case.reopen().process_job(tenant_id=TENANT, job_id=job_id)
        status = await client.get(f"/api/v1/knowledge/index-jobs/{job_id}", headers=headers)
        assert status.json()["status"] == "complete" and not status.json()["cleanup_dirty"]
    await assert_deleted(case, source)
    for keep in (replacement, unrelated, other_tenant):
        assert await case.raw.verify_records(tier=VectorTier(keep["target_tier"]),
            records=[record_from_dict(value) for value in keep["records"]])
    contents = await case.content.batch_get_by_news_ids(tenant_id=TENANT, news_ids=[source["news_id"]])
    assert contents[source["news_id"]].body == case.rows[0]["body"]
    assert contents[source["news_id"]].news_id == source["news_id"]
    with pytest.raises(ValueError, match="restore"):
        await case.service.reheat(tenant_id=TENANT, news_id=source["news_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["extra_fact", "negation", "number", "date", "title", "unrelated", "same_event", "short"])
async def test_complementary_or_conflicting_news_must_not_be_deleted(business, change):
    case = business
    original = doc(case)
    copy = doc(case, copy=True)
    if change == "extra_fact":
        copy = replace(copy, text=copy.text + "\n测试新增信息：这是另一份独立采访，不能当作已验收的事实覆盖。")
    elif change == "negation":
        copy = replace(copy, text="测试否定说明：并未发生。" + copy.text)
    elif change == "number":
        copy = replace(copy, text=copy.text + "\n测试数字：原金额应为 300 万元。")
    elif change == "date":
        copy = replace(copy, metadata={**copy.metadata, "publish_time":
            (datetime.fromisoformat(copy.metadata["publish_time"]) - timedelta(days=365)).isoformat()})
    elif change == "title":
        copy = replace(copy, title="测试更正：" + copy.title)
    elif change == "unrelated":
        copy = doc(case, row=1)
    elif change == "same_event":
        # Two stored Tencent Q2 reports, each with its original independent body.
        original, copy = doc(case, row=1), doc(case, row=2)
    else:
        original = replace(original, text="内容不足的短稿")
        copy = replace(copy, text=original.text)
    source, target = await index(case, original), await index(case, copy)
    with pytest.raises(ValueError, match="strict same-day duplicates"):
        await case.service.retire_duplicate(**request_for(source, target))
    assert (await case.ledger.get_news(tenant_id=TENANT, news_id=source["news_id"]))["status"] == "active"
    assert await case.raw.contains(tier=VectorTier(source["target_tier"]), chunk_ids=source["chunk_ids"])


class FaultIndex:
    def __init__(self, inner, mode):
        self.inner, self.mode = inner, mode

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def delete(self, *, tier, chunk_ids):
        if self.mode == "timeout":
            raise TimeoutError("injected transport failure")
        if self.mode == "false_ack":
            return len(chunk_ids)
        await self.inner.delete(tier=tier, chunk_ids=chunk_ids[:1])
        return len(chunk_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["timeout", "false_ack", "partial"])
async def test_delete_failure_hides_stale_hits_and_recovers_after_restart(business, fault):
    case = business
    # A small chunk size ensures that a partial delete leaves physical rows.
    case.service.config = case.config.model_copy(update={"chunk_chars": 80, "chunk_overlap": 10})
    source = await index(case, doc(case))
    target = await index(case, doc(case, copy=True))
    job = await case.service.retire_duplicate(**request_for(source, target))
    with pytest.raises((RuntimeError, TimeoutError)):
        await case.reopen(FaultIndex(case.raw, fault)).process_job(tenant_id=TENANT, job_id=job["job_id"])
    saved = await case.ledger.get_job(tenant_id=TENANT, job_id=source["job_id"])
    assert saved["cleanup_dirty"] and saved["last_error"].startswith("Cleanup")
    assert await found(case) == {target["news_id"]}
    assert await case.raw.contains(tier=VectorTier(source["target_tier"]), chunk_ids=source["chunk_ids"])
    await case.reopen().process_job(tenant_id=TENANT, job_id=job["job_id"])
    await assert_deleted(case, source)
    assert not (await case.ledger.get_job(tenant_id=TENANT, job_id=source["job_id"]))["cleanup_dirty"]
    # An abandoned network write returns after cleanup: no resurrection.
    await case.raw.upsert(tier=VectorTier(source["target_tier"]),
        records=[record_from_dict(value) for value in source["records"]])
    await case.reopen().process_job(tenant_id=TENANT, job_id=source["job_id"])
    assert await found(case) == {target["news_id"]}
    await assert_deleted(case, source)


@pytest.mark.asyncio
async def test_restore_creates_new_projection_and_late_cleanup_cannot_delete_it(business):
    case = business
    source = await index(case, doc(case))
    target = await index(case, doc(case, copy=True))
    retirement = await case.service.retire_duplicate(**request_for(source, target))
    await case.service.process_job(tenant_id=TENANT, job_id=retirement["job_id"])
    restored = await case.reopen().restore_duplicate(tenant_id=TENANT, news_id=source["news_id"],
        retirement_job_id=retirement["job_id"], now=case.now)
    again = await case.service.restore_duplicate(tenant_id=TENANT, news_id=source["news_id"],
        retirement_job_id=retirement["job_id"], now=case.now)
    assert again["job_id"] == restored["job_id"]
    await case.service.process_job(tenant_id=TENANT, job_id=restored["job_id"])
    await case.service.process_job(tenant_id=TENANT, job_id=retirement["job_id"])
    saved = await case.ledger.get_job(tenant_id=TENANT, job_id=restored["job_id"])
    assert set(saved["chunk_ids"]).isdisjoint(source["chunk_ids"])
    assert await found(case) == {source["news_id"], target["news_id"]}
    # A newer withdrawal still dominates any restore request.
    withdrawn = await case.service.retract(tenant_id=TENANT, news_id=source["news_id"], source_revision=2)
    await case.service.process_job(tenant_id=TENANT, job_id=withdrawn["job_id"])
    with pytest.raises(ValueError, match="source revision"):
        await case.service.restore_duplicate(tenant_id=TENANT, news_id=source["news_id"],
            retirement_job_id=retirement["job_id"], now=case.now)


@pytest.mark.asyncio
async def test_unready_or_changed_replacement_cannot_authorize_deletion(business):
    case = business
    source = await index(case, doc(case))
    target = await case.service.accept(tenant_id=TENANT, document=doc(case, copy=True), now=case.now)
    with pytest.raises(ValueError, match="not ready"):
        await case.service.retire_duplicate(**request_for(source, target))
    await case.service.process_job(tenant_id=TENANT, job_id=target["job_id"])
    target = await case.ledger.get_job(tenant_id=TENANT, job_id=target["job_id"])
    await case.raw.delete(tier=VectorTier(target["target_tier"]), chunk_ids=target["chunk_ids"][:1])
    with pytest.raises(ValueError, match="verification failed"):
        await case.service.retire_duplicate(**request_for(source, target))
    await case.raw.upsert(tier=VectorTier(target["target_tier"]),
        records=[record_from_dict(value) for value in target["records"]])

    class WithdrawalDuringVerification:
        async def verify_records(self, **kwargs):
            result = await case.raw.verify_records(**kwargs)
            await case.service.retract(tenant_id=TENANT, news_id=target["news_id"], source_revision=2)
            return result

    with pytest.raises(ValueError, match="remain active"):
        await case.reopen(WithdrawalDuringVerification()).retire_duplicate(**request_for(source, target))
    assert await found(case) == {source["news_id"]}


@pytest.mark.asyncio
async def test_concurrent_retries_have_one_retirement_and_reverse_requests_cannot_delete_both(business):
    case = business
    if case.backend != "postgres":
        pytest.skip("real advisory-lock contention requires PostgreSQL")
    source = await index(case, doc(case))
    target = await index(case, doc(case, copy=True))
    results = await asyncio.gather(*(case.service.retire_duplicate(**request_for(source, target)) for _ in range(8)))
    assert len({value["job_id"] for value in results}) == 1
    with pytest.raises(ValueError):
        await case.service.retire_duplicate(**request_for(target, source))
    await case.service.process_job(tenant_id=TENANT, job_id=results[0]["job_id"])
    assert await found(case) == {target["news_id"]}


@pytest.mark.asyncio
async def test_revision_and_tenant_guards_reject_stale_delete_intent(business):
    case = business
    source = await index(case, doc(case))
    target = await index(case, doc(case, copy=True), tenant=OTHER)
    with pytest.raises(ValueError, match="this tenant"):
        await case.service.retire_duplicate(**request_for(source, target))
    target = await index(case, doc(case, copy=True))
    replacement_request = request_for(source, target)
    newer = await index(case, doc(case, revision=2))
    with pytest.raises(ValueError, match="revision"):
        await case.service.retire_duplicate(**replacement_request)
    assert await case.raw.contains(tier=VectorTier(newer["target_tier"]), chunk_ids=newer["chunk_ids"])


@pytest.mark.asyncio
async def test_simultaneous_opposite_retirements_cannot_remove_last_representative(business):
    case = business
    if case.backend != "postgres":
        pytest.skip("real advisory-lock contention requires PostgreSQL")
    first = await index(case, doc(case))
    second = await index(case, doc(case, copy=True))
    results = await asyncio.gather(case.service.retire_duplicate(**request_for(first, second)),
        case.service.retire_duplicate(**request_for(second, first)), return_exceptions=True)
    accepted = [value for value in results if isinstance(value, dict)]
    assert len(accepted) == 1
    assert sum(isinstance(value, ValueError) for value in results) == 1
    await case.service.process_job(tenant_id=TENANT, job_id=accepted[0]["job_id"])
    assert len(await found(case)) == 1
