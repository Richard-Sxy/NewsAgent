"""Always-on safety tests; public-corpus/PostgreSQL replay is a separate suite."""
from datetime import timedelta

import pytest

from app.modules.knowledge.duplicate_policy import duplicate_evidence
from app.modules.knowledge.tier_lifecycle import record_from_dict
from app.retrieval.tiered_vector import VectorTier
from tests.test_tier_lifecycle_runtime import runtime, document, index_document, hits, TENANT, NOW


BODY = "这是测试新闻正文，金额为 100 万元，日期为 2026 年 10 月 15 日，主体尚未签订合同。" * 3


def intent(old, new):
    return dict(tenant_id=TENANT, news_id=old["news_id"], source_revision=old["source_revision"],
        source_job_id=old["job_id"], replacement_news_id=new["news_id"],
        replacement_revision=new["source_revision"], replacement_job_id=new["job_id"])


@pytest.mark.parametrize("field,value", [
    ("body", BODY.replace("100", "200")), ("body", BODY.replace("尚未", "已经")),
    ("body", BODY + "独家补充事实。"), ("title", "另一则新闻"),
    ("publish_time", NOW - timedelta(days=1)), ("body", "短稿"),
])
def test_duplicate_rule_does_not_drop_numbers_negation_date_or_complement(field, value):
    source = dict(body=BODY, title="测试标题", publish_time=NOW)
    with pytest.raises(ValueError, match="strict same-day duplicates"):
        duplicate_evidence(source, {**source, field: value})


@pytest.mark.asyncio
async def test_duplicate_retirement_fences_old_replay_and_explicit_restore_is_safe(runtime):
    old = await index_document(runtime, document("old", text=BODY))
    new = await index_document(runtime, document("new", text=BODY))
    retired = await runtime.service.retire_duplicate(**intent(old, new))
    assert (await runtime.service.retire_duplicate(**intent(old, new)))["job_id"] == retired["job_id"]
    assert {hit.news_id for hit in await hits(runtime)} == {"new"}
    await runtime.reopen().process_job(tenant_id=TENANT, job_id=retired["job_id"])
    assert not await runtime.raw_index.contains(tier=VectorTier.HOT, chunk_ids=old["chunk_ids"])
    assert await runtime.ledger.request_migration(tenant_id=TENANT, news_id="old", target_tier="cold") is None
    await runtime.service.process_job(tenant_id=TENANT, job_id=old["job_id"])
    assert not await runtime.ledger.activate(tenant_id=TENANT, job_id=old["job_id"])
    restored = await runtime.service.restore_duplicate(tenant_id=TENANT, news_id="old",
        retirement_job_id=retired["job_id"], now=NOW)
    await runtime.service.process_job(tenant_id=TENANT, job_id=restored["job_id"])
    await runtime.service.process_job(tenant_id=TENANT, job_id=retired["job_id"])
    # Constant test embeddings tie every chunk. Assert visibility of each news
    # independently instead of assuming a tiny candidate budget covers both.
    for wanted, excluded in (("old", "new"), ("new", "old")):
        result = await runtime.guarded.search(tier=VectorTier.HOT, tenant_id=TENANT,
            vector=(1.0, 0.0), embedding_version=runtime.config.embedding_version,
            exclude_news_ids=frozenset({excluded}), limit=1)
        assert {hit.news_id for hit in result} == {wanted}


@pytest.mark.asyncio
async def test_noop_delete_stays_dirty_until_verified_and_late_writes_are_cleaned(runtime):
    old = await index_document(runtime, document("old", text=BODY))
    new = await index_document(runtime, document("new", text=BODY))
    retired = await runtime.service.retire_duplicate(**intent(old, new))

    class FalseAck:
        async def delete(self, **kwargs):
            return len(kwargs["chunk_ids"])

        async def contains(self, **kwargs):
            return await runtime.raw_index.contains(**kwargs)

    with pytest.raises(RuntimeError, match="deletion verification"):
        await runtime.reopen(FalseAck()).process_job(tenant_id=TENANT, job_id=retired["job_id"])
    assert (await runtime.ledger.get_job(tenant_id=TENANT, job_id=old["job_id"]))["cleanup_dirty"]
    await runtime.reopen().process_job(tenant_id=TENANT, job_id=retired["job_id"])
    assert not (await runtime.ledger.get_job(tenant_id=TENANT, job_id=old["job_id"]))["cleanup_dirty"]
    # Completed cleanup does not prevent a late write. A later scan removes it.
    await runtime.raw_index.upsert(tier=VectorTier.HOT,
        records=[record_from_dict(value) for value in old["records"]])
    await runtime.service.scan_page(now=NOW)
    assert not await runtime.raw_index.contains(tier=VectorTier.HOT, chunk_ids=old["chunk_ids"])


@pytest.mark.asyncio
async def test_source_correction_and_pending_migration_block_old_retirement_request(runtime):
    old = await index_document(runtime, document("old", text=BODY))
    new = await index_document(runtime, document("new", text=BODY))
    migration = await runtime.ledger.request_migration(tenant_id=TENANT, news_id="old", target_tier="cold")
    with pytest.raises(ValueError, match="no pending change"):
        await runtime.service.retire_duplicate(**intent(old, new))
    await runtime.service.process_job(tenant_id=TENANT, job_id=migration["job_id"])
    await runtime.service.accept(tenant_id=TENANT, document=document("old", version=2, text=BODY), now=NOW)
    with pytest.raises(ValueError, match="revision"):
        await runtime.service.retire_duplicate(**intent(old, new))


@pytest.mark.asyncio
async def test_failed_index_job_can_be_physically_clean_without_erasing_its_failure(runtime):
    old = await runtime.service.accept(tenant_id=TENANT, document=document(text=BODY), now=NOW)
    records = await runtime.service._records(old)
    await runtime.raw_index.upsert(tier=VectorTier.HOT, records=[record_from_dict(value) for value in records])
    await runtime.ledger.record_error(tenant_id=TENANT, job_id=old["job_id"], error="IndexFailure", retryable=False)
    withdrawal = await runtime.service.retract(tenant_id=TENANT, news_id="story", source_revision=2)
    await runtime.service.process_job(tenant_id=TENANT, job_id=withdrawal["job_id"])
    saved = await runtime.ledger.get_job(tenant_id=TENANT, job_id=old["job_id"])
    assert saved["status"] == "failed" and saved["last_error"] == "IndexFailure"
    assert saved["cleanup_dirty"] is False
    assert not await runtime.raw_index.contains(tier=VectorTier.HOT, chunk_ids=saved["chunk_ids"])


@pytest.mark.asyncio
async def test_retirement_transaction_failure_preserves_both_active_projections(runtime):
    from sqlalchemy import event

    old = await index_document(runtime, document("old", text=BODY))
    new = await index_document(runtime, document("new", text=BODY))
    engine = runtime.database.engine

    def fail_update(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE tier_lifecycle_news"):
            raise RuntimeError("injected transaction failure after job insert")

    event.listen(engine, "before_cursor_execute", fail_update)
    try:
        with pytest.raises(RuntimeError, match="transaction failure"):
            await runtime.service.retire_duplicate(**intent(old, new))
    finally:
        event.remove(engine, "before_cursor_execute", fail_update)
    assert await runtime.ledger.list_pending() == []
    for job in (old, new):
        row = await runtime.ledger.get_news(tenant_id=TENANT, news_id=job["news_id"])
        assert row["status"] == "active" and row["current_job_id"] == job["job_id"]
        assert await runtime.raw_index.contains(tier=VectorTier.HOT, chunk_ids=job["chunk_ids"])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["embedding_version", "zero_tier_weight"])
async def test_replacement_must_be_eligible_for_current_query_configuration(runtime, change):
    old = await index_document(runtime, document("old", text=BODY))
    new = await index_document(runtime, document("new", text=BODY))
    update = ({"embedding_version": "another-model"} if change == "embedding_version" else
              {"query_weights": {VectorTier.HOT: 0.0, VectorTier.WARM: 1.0, VectorTier.COLD: 1.0}})
    runtime.service.config = runtime.config.model_copy(update=update)
    with pytest.raises(ValueError, match="query configuration"):
        await runtime.service.retire_duplicate(**intent(old, new))
    assert (await runtime.ledger.get_news(tenant_id=TENANT, news_id="old"))["status"] == "active"
