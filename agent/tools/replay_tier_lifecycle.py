#!/usr/bin/env python3
"""Run a persistent, network-free three-tier lifecycle acceptance replay.

The synthetic articles, hash embedding and SQLite exact scan exercise lifecycle
correctness, not semantic relevance, ANN performance, PostgreSQL concurrency or
Temporal dispatch. Business time advances explicitly; trace milliseconds always
come from the real monotonic clock in LifecycleKnowledgeSearch.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from app.modules.knowledge.document import KnowledgeDocument
from app.modules.knowledge.tier_lifecycle import KnowledgeLifecycleService
from app.modules.knowledge.tier_lifecycle_config import load_knowledge_lifecycle_config
from app.modules.knowledge.tier_lifecycle_index import GuardedTieredIndex
from app.modules.knowledge.tier_lifecycle_local_index import LocalLifecycleVectorIndex
from app.modules.knowledge.tier_lifecycle_query import LifecycleKnowledgeSearch
from app.modules.knowledge.tier_lifecycle_store import TierLifecycleStore, _metadata, _vectors
from app.model_runtime.local_embedding import LocalHashEmbedding
from app.model_runtime.query_embedding import ModelQueryEmbedder
from app.retrieval.tiered_vector import VectorTier


DEFAULT_CONFIG = AGENT_ROOT / "deploy" / "knowledge-lifecycle.local.example.yml"
LOCAL_MODEL = "native-local-embedding-v1"
TENANT = "tier-replay-synthetic"
FIXED_NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


class LocalReplayDatabase:
    """Synchronous SQLite transactions exposed through the existing async Port.

    Each call executes immediately in this single-process simulator. This is
    intentionally not an async database throughput or concurrent-locking test.
    """

    def __init__(self, path: Path):
        self.path = path
        self.engine = create_engine(f"sqlite:///{path.resolve()}")
        _metadata.create_all(self.engine)

    @asynccontextmanager
    async def session(self):
        with Session(self.engine) as current:
            async def execute(*args, **kwargs):
                return current.execute(*args, **kwargs)
            try:
                yield SimpleNamespace(execute=execute)
                current.commit()
            except BaseException:
                current.rollback()
                raise

    def physical_vectors(self) -> list[dict]:
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(
                select(_vectors).order_by(_vectors.c.chunk_id)
            ).mappings()]

    def close(self):
        self.engine.dispose()


def build_runtime(database: LocalReplayDatabase, config):
    ledger = TierLifecycleStore(database=database)
    vectors = LocalLifecycleVectorIndex(database=database,
        max_search_rows=config.max_search_rows, environment="e2e")
    embedding = LocalHashEmbedding(model_routes=(LOCAL_MODEL,),
                                   dimensions=config.expected_embedding_dimensions)
    guarded = GuardedTieredIndex(vectors, ledger,
        max_candidates=config.query_max_candidates, refill_factor=config.query_refill_factor)
    service = KnowledgeLifecycleService(ledger=ledger, index=guarded, embedding=embedding, config=config)
    search = LifecycleKnowledgeSearch(index=guarded, ledger=ledger,
        embedder=ModelQueryEmbedder(embedding, version=LOCAL_MODEL), config=config)
    return SimpleNamespace(database=database, ledger=ledger, vectors=vectors,
                           service=service, search=search)


def synthetic_documents(config) -> dict[str, KnowledgeDocument]:
    ages = {
        "seed-hot": config.hot_max_age_seconds / 2,
        "seed-warm": (config.hot_max_age_seconds + config.cold_min_age_seconds) / 2,
        "seed-cold": config.cold_min_age_seconds + config.hot_max_age_seconds,
    }
    return {news_id: KnowledgeDocument(
        document_id=f"document-{news_id}", title=f"模拟新闻：{news_id}",
        text=f"这是一篇用于分层状态测试的合成新闻。编号为{news_id}，内容版本为一。",
        media_type="article", text_source="synthetic-lifecycle-replay",
        source_url=f"https://example.invalid/{news_id}",
        metadata={"news_id": news_id, "content_version": "1",
                  "publish_time": (FIXED_NOW - timedelta(seconds=age)).isoformat()},
    ) for news_id, age in ages.items()}


async def replay(*, config_path: Path, output_dir: Path) -> dict:
    """Create a fresh replay directory; never overwrite previous evidence."""
    config = load_knowledge_lifecycle_config(config_path)
    if config.embedding_version != LOCAL_MODEL or config.expected_embedding_dimensions < 4:
        raise ValueError("replay requires native-local-embedding-v1 with at least 4 dimensions")
    if config.query_max_candidates < 3:
        raise ValueError("replay requires query_max_candidates >= 3 for all three seed articles")
    if output_dir.is_symlink() or (output_dir.exists() and (
            not output_dir.is_dir() or any(output_dir.iterdir()))):
        raise ValueError("output directory must be new or empty; previous evidence is never overwritten")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir = output_dir.resolve()
    db_path = output_dir / "lifecycle.sqlite3"
    report_path = output_dir / "report.json"
    with (output_dir / "config.snapshot.json").open("x", encoding="utf-8") as stream:
        json.dump(config.model_dump(mode="json"), stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    report = {
        "schema_version": 1, "status": "running", "tenant_id": TENANT,
        "business_clock_start": FIXED_NOW.isoformat(),
        "config": config.model_dump(mode="json"),
        "measurement_scope": {
            "model": "local deterministic hash embedding",
            "index": "SQLite persisted vectors with exact inner-product scan",
            "network_calls": False, "temporal_dispatch_exercised": False,
            "ann_performance": False, "enterprise_model_quality": False,
            "postgres_concurrency": False,
            "timing_clock": "real monotonic perf_counter; business age uses explicit UTC clock",
        },
        "database_path": str(db_path), "steps": [], "checks": [],
    }
    database = LocalReplayDatabase(db_path)
    runtime = build_runtime(database, config)

    def check(name: str, condition: bool):
        report["checks"].append({"name": name, "passed": bool(condition)})
        if not condition:
            raise AssertionError(f"lifecycle replay check failed: {name}")

    async def state():
        rows = await runtime.ledger.list_news(limit=10, include_retracted=True)
        keys = ("news_id", "source_revision", "generation", "status", "active_tier",
                "desired_tier", "active_job_id", "current_job_id", "hold_until")
        return [{key: row[key] for key in keys} for row in rows]

    async def capture(name: str, now: datetime):
        result = await runtime.search.search(query_text="模拟新闻 内容版本 分层测试",
                                              tenant_id=TENANT, limit=3)
        rows = await state()
        counts = {tier.value: sum(row["status"] == "active" and row["active_tier"] == tier.value
                                 for row in rows) for tier in VectorTier}
        vectors = runtime.database.physical_vectors()
        step = {"name": name, "business_time": now.isoformat(), "state": rows,
            "active_news_counts": counts,
            "physical_vector_counts": {tier.value: sum(row["tier"] == tier.value for row in vectors)
                                       for tier in VectorTier},
            "query_hits": [{"news_id": hit.news_id, "content_version": hit.content_version,
                            "tier": hit.tier.value, "chunk_id": hit.chunk_id} for hit in result["hits"]],
            "trace": result["trace"]}
        report["steps"].append(step)
        return step

    async def process(job):
        if job is None:
            raise AssertionError("expected a durable job")
        result = await runtime.service.process_job(tenant_id=TENANT, job_id=job["job_id"])
        check(f"job_complete:{job['kind']}:{job['news_id']}:{job['generation']}",
              result["status"] == "complete")

    async def scan_and_process(now):
        cursor = None
        while True:
            cursor = await runtime.service.scan_page(now=now, after_key=cursor)
            if cursor is None:
                break
        # Process each pending job at most once in this pass. Newly dirtied old
        # jobs are reconciled by service itself and can be swept again later.
        cursor = None
        while True:
            jobs = await runtime.ledger.list_pending(limit=100, after_job_id=cursor)
            if not jobs:
                break
            for job in jobs:
                await process(job)
            cursor = jobs[-1]["job_id"]

    async def reject_event(name, call):
        try:
            await call()
        except ValueError:
            check(name, True)
        else:
            check(name, False)

    try:
        docs = synthetic_documents(config)
        seed_jobs = {}
        for news_id, document in docs.items():
            job = await runtime.service.accept(tenant_id=TENANT, document=document, now=FIXED_NOW)
            seed_jobs[news_id] = job
            await process(job)
        seeded = await capture("seed_three_tiers", FIXED_NOW)
        check("one_seed_article_in_each_tier", seeded["active_news_counts"] == {"hot": 1, "warm": 1, "cold": 1})
        check("seed_query_returns_all_three", {hit["news_id"] for hit in seeded["query_hits"]} == set(docs))

        duplicate = await runtime.service.accept(tenant_id=TENANT, document=docs["seed-hot"], now=FIXED_NOW)
        check("duplicate_content_reuses_job", duplicate["job_id"] == seed_jobs["seed-hot"]["job_id"])

        corrected = replace(docs["seed-cold"], text="合成新闻更正后的内容，版本为二。旧内容必须停止返回。",
            metadata={**docs["seed-cold"].metadata, "content_version": "2"})
        update_job = await runtime.service.accept(tenant_id=TENANT, document=corrected, now=FIXED_NOW)
        pending = await capture("correction_pending_hides_old", FIXED_NOW)
        check("old_revision_hidden_before_reindex", "seed-cold" not in {hit["news_id"] for hit in pending["query_hits"]})
        before_restart = await state()
        database.close()
        database = LocalReplayDatabase(db_path)
        runtime = build_runtime(database, config)
        check("pending_job_and_news_survive_restart", await state() == before_restart)
        persisted = await runtime.ledger.get_job(tenant_id=TENANT, job_id=update_job["job_id"])
        check("pending_job_payload_survives_restart", persisted is not None and persisted["payload"]["body"] == corrected.text)
        await process(persisted)
        updated = await capture("correction_ready_after_restart", FIXED_NOW)
        check("corrected_news_returns_only_v2", [hit["content_version"] for hit in updated["query_hits"]
                                                if hit["news_id"] == "seed-cold"] == [2])
        await reject_event("out_of_order_content_rejected", lambda: runtime.service.accept(
            tenant_id=TENANT, document=docs["seed-cold"], now=FIXED_NOW))
        await reject_event("same_revision_changed_body_rejected", lambda: runtime.service.accept(
            tenant_id=TENANT, document=replace(corrected, text="同一版本的冲突内容"), now=FIXED_NOW))

        reheat_job = await runtime.service.reheat(tenant_id=TENANT, news_id="seed-cold", now=FIXED_NOW)
        await process(reheat_job)
        reheated = await capture("cold_news_reheated_to_hot", FIXED_NOW)
        check("reheated_news_has_one_hot_result", [(hit["tier"], hit["content_version"])
            for hit in reheated["query_hits"] if hit["news_id"] == "seed-cold"] == [("hot", 2)])

        hot_published = datetime.fromisoformat(docs["seed-hot"].metadata["publish_time"])
        warm_now = hot_published + timedelta(seconds=(config.hot_max_age_seconds + config.cold_min_age_seconds)/2)
        await scan_and_process(warm_now)
        warmed = await capture("age_hot_to_warm", warm_now)
        check("seed_hot_aged_to_warm", next(row for row in warmed["state"] if row["news_id"] == "seed-hot")["active_tier"] == "warm")
        cold_now = hot_published + timedelta(seconds=config.cold_min_age_seconds)
        await scan_and_process(cold_now)
        cooled = await capture("age_warm_to_cold", cold_now)
        check("seed_hot_aged_to_cold", next(row for row in cooled["state"] if row["news_id"] == "seed-hot")["active_tier"] == "cold")
        expired_now = max(cold_now, FIXED_NOW + timedelta(seconds=config.reheat_hold_seconds + 1))
        await scan_and_process(expired_now)
        expired = await capture("reheat_hold_expired", expired_now)
        check("reheat_hold_resumes_age_policy", next(row for row in expired["state"] if row["news_id"] == "seed-cold")["active_tier"] == "cold")

        retract_job = await runtime.service.retract(tenant_id=TENANT, news_id="seed-cold", source_revision=3)
        check("withdraw_test_has_vectors_before_cleanup", any(row["record"]["news_id"] == "seed-cold"
                                                               for row in database.physical_vectors()))
        withdrawn = await capture("withdraw_blocks_before_cleanup", expired_now)
        check("withdraw_gate_is_immediate", "seed-cold" not in {hit["news_id"] for hit in withdrawn["query_hits"]})
        await process(retract_job)
        duplicate_retract = await runtime.service.retract(tenant_id=TENANT, news_id="seed-cold", source_revision=3)
        check("duplicate_withdraw_reuses_job", duplicate_retract["job_id"] == retract_job["job_id"])
        await process(update_job)  # A delayed old task must not re-activate content.
        await reject_event("withdrawn_news_rejects_old_content", lambda: runtime.service.accept(
            tenant_id=TENANT, document=corrected, now=expired_now))
        await reject_event("withdrawn_news_cannot_reheat", lambda: runtime.service.reheat(
            tenant_id=TENANT, news_id="seed-cold", now=expired_now))
        check("withdraw_cleanup_removes_physical_vectors", not any(row["record"]["news_id"] == "seed-cold"
                                                                   for row in database.physical_vectors()))

        final_before_restart = await capture("withdraw_complete", expired_now)
        before_restart = await state()
        vectors_before_restart = database.physical_vectors()
        database.close()
        database = LocalReplayDatabase(db_path)
        runtime = build_runtime(database, config)
        final = await capture("final_restart_preserves_serving_state", expired_now)
        check("final_state_survives_restart", await state() == before_restart)
        check("physical_projections_survive_restart", database.physical_vectors() == vectors_before_restart)
        check("query_results_survive_restart", final["query_hits"] == final_before_restart["query_hits"])
        check("withdrawn_news_stays_absent_after_restart", "seed-cold" not in {hit["news_id"] for hit in final["query_hits"]})
        isolated = await runtime.search.search(query_text="模拟新闻", tenant_id="another-synthetic-tenant", limit=3)
        check("tenant_isolation", isolated["hits"] == [])
        report["status"] = "passed"
        report["final_active_news_counts"] = final["active_news_counts"]
        return report
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        database.close()
        with report_path.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Explicit local lifecycle policy YAML; no runtime endpoint config is read")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="New or empty directory for SQLite, frozen config and JSON acceptance report")
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(replay(config_path=args.config, output_dir=args.output_dir))
    except (ValueError, OSError, AssertionError, RuntimeError) as exc:
        print(f"Replay failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": report["status"], "checks": len(report["checks"]),
        "report": str((args.output_dir / "report.json").resolve()),
        "scope": "local deterministic correctness replay; not ANN performance"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
