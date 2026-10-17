"""Durable content -> embedding -> verified projection -> conditional activation.

All external calls happen outside ledger transactions. A job owns immutable
chunk IDs; retries and delayed cleanup cannot overwrite a newer projection.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import math
import logging

from app.observability.decorators import traced
from app.knowledge.document import IngestOutcome, IngestReport, IngestStatus
from app.knowledge.native_indexing import split_text, require_embedding_dimension
from app.knowledge.tier_lifecycle_config import KnowledgeLifecycleConfig
from app.knowledge.tier_lifecycle_store import projection_chunk_id
from app.model_runtime.core import EmbeddingRequest
from app.retrieval.tiered_vector import ChunkVectorRecord, VectorTier


log = logging.getLogger(__name__)


def record_to_dict(record: ChunkVectorRecord) -> dict:
    value = asdict(record)
    value["publish_time"] = record.publish_time.isoformat()
    value["vector"] = list(record.vector)
    return value


def record_from_dict(value: dict) -> ChunkVectorRecord:
    return ChunkVectorRecord(**{**value,
        "publish_time": datetime.fromisoformat(value["publish_time"]),
        "vector": tuple(value["vector"])})


class KnowledgeLifecycleService:
    def __init__(self, *, ledger, index, embedding, config: KnowledgeLifecycleConfig):
        self.ledger, self.index, self.embedding, self.config = ledger, index, embedding, config

    async def accept(self, *, tenant_id: str, document, now: datetime | None = None) -> dict:
        document.validate()
        now = now or datetime.now(UTC)
        news_id = document.metadata.get("news_id", "").strip()
        revision = int(document.metadata.get("content_version", "0"))
        if not news_id or revision < 1:
            raise ValueError("news_id and positive content_version are required")
        published = datetime.fromisoformat(document.metadata.get("publish_time", ""))
        tier = self.config.classify_tier(published, now)
        return await self.ledger.accept_content(
            tenant_id=tenant_id, document_id=document.document_id, news_id=news_id,
            source_revision=revision, title=document.title, body=document.text,
            publish_time=published, source_url=document.source_url or None,
            desired_tier=tier.value, config=self.config.model_dump(mode="json"))

    async def upsert_documents(self, *, tenant_id, documents) -> IngestReport:
        outcomes = []
        for document in documents:
            try:
                previous = await self.ledger.get_news(
                    tenant_id=tenant_id, news_id=document.metadata.get("news_id", ""))
                job = await self.accept(tenant_id=tenant_id, document=document)
                duplicate = previous is not None and previous["source_revision"] == job["source_revision"]
                outcomes.append(IngestOutcome(document.document_id,
                    IngestStatus.SKIPPED if duplicate else (IngestStatus.UPDATED if previous else IngestStatus.CREATED),
                    collection_id=job["job_id"], detail="accepted; inspect document status for indexing readiness"))
            except (ValueError, TypeError) as exc:
                outcomes.append(IngestOutcome(document.document_id, IngestStatus.FAILED, detail=str(exc)))
        return IngestReport(tuple(outcomes))

    async def get_document_status(self, *, tenant_id, document_id):
        row = await self.ledger.get_document(tenant_id=tenant_id, document_id=document_id)
        if row is None:
            return None
        current_job = await self.ledger.get_job(tenant_id=tenant_id, job_id=row["current_job_id"])
        return {"document_id": document_id, "news_id": row["news_id"],
            "content_version": row["source_revision"], "status": "ready" if row["status"] == "active" else row["status"],
            "active_tier": row["active_tier"], "desired_tier": row["desired_tier"],
            "active_job_id": row["active_job_id"], "generation": row["generation"],
            "current_job_id": row["current_job_id"], "index_status": current_job["status"],
            "last_error": current_job["last_error"]}

    async def retract(self, *, tenant_id, news_id, source_revision):
        return await self.ledger.retract(tenant_id=tenant_id, news_id=news_id, source_revision=source_revision)

    async def reheat(self, *, tenant_id, news_id, now=None):
        now = now or datetime.now(UTC)
        row = await self.ledger.get_news(tenant_id=tenant_id, news_id=news_id)
        if row is None or row["status"] == "retracted":
            raise ValueError("cannot reheat unknown or retracted news")
        policy = KnowledgeLifecycleConfig.model_validate(row["config"])
        return await self.ledger.request_migration(tenant_id=tenant_id, news_id=news_id,
            target_tier="hot", expected_generation=row["generation"],
            hold_until=now + timedelta(seconds=policy.reheat_hold_seconds))

    async def _records(self, job: dict) -> list[dict]:
        cfg = KnowledgeLifecycleConfig.model_validate(job["config"])
        if (cfg.embedding_version != self.config.embedding_version
                or cfg.expected_embedding_dimensions != self.config.expected_embedding_dimensions):
            raise ValueError("job embedding model differs from worker; deploy matching worker")
        payload = job["payload"]
        # Bound the whole projection before materializing overlapping strings or
        # invoking the model. Per-RPC batch limits alone do not bound job memory.
        remaining = max(0, len(payload["body"].strip()) - cfg.chunk_chars)
        step = cfg.chunk_chars - cfg.chunk_overlap
        count = 1 + (remaining + step - 1) // step
        if (count > cfg.max_chunks_per_document
                or count * cfg.expected_embedding_dimensions > cfg.max_vector_coordinates_per_document):
            raise ValueError("document projection exceeds configured memory budget")
        chunks = split_text(payload["body"], max_chars=cfg.chunk_chars, overlap=cfg.chunk_overlap)
        if job["records"]:
            if (not self._valid_records(job, cfg, chunks, job["records"])
                    or job["chunk_ids"] != [record["chunk_id"] for record in job["records"]]):
                raise ValueError("saved projection does not match its immutable contract")
            return job["records"]
        reused = await self._reuse_vectors(job, cfg, chunks)
        if reused is not None:
            return await self._save_records(job, cfg, chunks, reused)
        vectors = []
        for start in range(0, len(chunks), cfg.embedding_batch_size):
            batch = chunks[start:start + cfg.embedding_batch_size]
            result = await self.embedding.embed(EmbeddingRequest(
                tenant_id=job["tenant_id"], trace_id=f"tier-{job['job_id']}-{start}",
                model_route=cfg.embedding_version, texts=tuple(f"{payload['title']}\n{part}" for part in batch)))
            if result.model_version != cfg.embedding_version or len(result.vectors) != len(batch):
                raise ValueError("embedding model or count drift")
            require_embedding_dimension(result.vectors, expected=cfg.expected_embedding_dimensions)
            if any(not math.isfinite(number) for vector in result.vectors for number in vector):
                raise ValueError("embedding contains non-finite coordinates")
            vectors.extend(result.vectors)
        records = [record_to_dict(ChunkVectorRecord(
            chunk_id=projection_chunk_id(job["job_id"], i), tenant_id=job["tenant_id"],
            news_id=job["news_id"], content_version=job["source_revision"], chunk_index=i,
            embedding_version=cfg.embedding_version, title=payload["title"], excerpt=part,
            source_url=payload["source_url"], publish_time=datetime.fromisoformat(payload["publish_time"]),
            vector=tuple(vectors[i]))) for i, part in enumerate(chunks)]
        return await self._save_records(job, cfg, chunks, records)

    def _valid_records(self, job, cfg, chunks, records) -> bool:
        """Validate all chunks, metadata and vector coordinates before reuse."""
        if not isinstance(records, list) or len(records) != len(chunks):
            return False
        for i, (record, part) in enumerate(zip(records, chunks, strict=True)):
            if (not isinstance(record, dict) or type(record.get("chunk_index")) is not int
                    or type(record.get("content_version")) is not int):
                return False
            vector = record.get("vector")
            if (not isinstance(vector, list) or len(vector) != cfg.expected_embedding_dimensions
                    or any(not isinstance(number, (float, int)) or isinstance(number, bool)
                           or not math.isfinite(number) for number in vector)):
                return False
            payload = job["payload"]
            expected = record_to_dict(ChunkVectorRecord(
                chunk_id=projection_chunk_id(job["job_id"], i), tenant_id=job["tenant_id"],
                news_id=job["news_id"], content_version=job["source_revision"], chunk_index=i,
                embedding_version=cfg.embedding_version, title=payload["title"], excerpt=part,
                source_url=payload["source_url"], publish_time=datetime.fromisoformat(payload["publish_time"]),
                vector=tuple(vector)))
            if record != expected:
                return False
        return True

    async def _reuse_vectors(self, job, cfg, chunks) -> list[dict] | None:
        if job["kind"] != "migrate":
            return None
        source = await self.ledger.reusable_source_job(tenant_id=job["tenant_id"], job_id=job["job_id"])
        if source is None:
            return None
        # Policy weights, age thresholds and RPC batch sizes do not change the
        # embedding input. Model/dimension and chunking contracts always do.
        contract = ("schema_version", "embedding_version", "expected_embedding_dimensions",
                    "chunking_version", "chunk_chars", "chunk_overlap")
        if (not isinstance(source["config"], dict)
                or any(source["config"].get(key) != getattr(cfg, key) for key in contract)):
            return None
        records = source["records"]
        if (not self._valid_records(source, cfg, chunks, records)
                or source["chunk_ids"] != [record["chunk_id"] for record in records]):
            return None
        # Never copy source IDs: cleanup owns projection IDs forever, including
        # when moving back into a collection that held an earlier projection.
        return [{**record, "chunk_id": projection_chunk_id(job["job_id"], i),
                 "vector": list(record["vector"])} for i, record in enumerate(records)]

    async def _save_records(self, job, cfg, chunks, records) -> list[dict]:
        saved = await self.ledger.save_records(tenant_id=job["tenant_id"], job_id=job["job_id"], records=records)
        # Another Activity attempt may have won the immutable first save.
        if not self._valid_records(job, cfg, chunks, saved["records"]):
            raise ValueError("saved projection does not match its immutable contract")
        return saved["records"]

    async def _delete(self, job: dict):
        ids = [record["chunk_id"] for record in job["records"]]
        cfg = KnowledgeLifecycleConfig.model_validate(job["config"]) if job.get("config") else self.config
        for start in range(0, len(ids), cfg.vector_batch_size):
            await self.index.delete(tier=VectorTier(job["target_tier"]),
                                    chunk_ids=ids[start:start+cfg.vector_batch_size])

    async def reconcile(self, *, tenant_id, news_id):
        # Repeated sweeps also remove late writes from abandoned SDK calls.
        for old in await self.ledger.obsolete_jobs(tenant_id=tenant_id, news_id=news_id):
            try:
                await self._delete(old)
            except Exception as exc:
                await self.ledger.record_maintenance_error(tenant_id=tenant_id, job_id=old["job_id"],
                    error="Cleanup" + type(exc).__name__, cleanup_dirty=True)
                raise
            await self.ledger.complete(tenant_id=tenant_id, job_id=old["job_id"])

    @traced("knowledge.index", identity={"job_id": "newsagent.job_id"})
    async def process_job(self, *, tenant_id, job_id):
        job = await self.ledger.get_job(tenant_id=tenant_id, job_id=job_id)
        if job is None:
            raise ValueError("unknown lifecycle job")
        if job["status"] == "failed":
            return {"job_id": job_id, "status": "failed"}
        cfg = self.config
        try:
            cfg = KnowledgeLifecycleConfig.model_validate(job["config"]) if job.get("config") else self.config
            row = await self.ledger.get_news(tenant_id=tenant_id, news_id=job["news_id"])
            current = row["generation"] == job["generation"] and row["status"] != "retracted"
            if job["kind"] == "retract" or not current:
                # Never touch the active projection, even after a later restore.
                await self.reconcile(tenant_id=tenant_id, news_id=job["news_id"])
            elif row["active_job_id"] == job_id:
                await self.reconcile(tenant_id=tenant_id, news_id=job["news_id"])
            else:
                records = [record_from_dict(value) for value in await self._records(job)]
                tier = VectorTier(job["target_tier"])
                for start in range(0, len(records), cfg.vector_batch_size):
                    batch = records[start:start+cfg.vector_batch_size]
                    await self.index.upsert(tier=tier, records=batch)
                    if not await self.index.verify_records(tier=tier, records=batch):
                        raise RuntimeError("target projection verification failed")
                await self.ledger.activate(tenant_id=tenant_id, job_id=job_id)
                # A competing attempt may already have activated this job, then
                # exhausted cleanup retries. A failed CAS is not deletion authority.
                # Only the ledger's obsolete set may be physically removed.
                await self.reconcile(tenant_id=tenant_id, news_id=job["news_id"])
            await self.ledger.complete(tenant_id=tenant_id, job_id=job_id)
            finished = await self.ledger.get_job(tenant_id=tenant_id, job_id=job_id)
            return {"job_id": job_id, "status": finished["status"]}
        except Exception as exc:
            # Store only an error class, never potentially sensitive SDK messages.
            retryable = not isinstance(exc, (ValueError, TypeError)) and job["attempts"] + 1 < cfg.max_attempts
            await self.ledger.record_error(tenant_id=tenant_id, job_id=job_id,
                                          error=type(exc).__name__, retryable=retryable)
            raise

    async def scan_page(self, *, now=None, after_key=None):
        now = now or datetime.now(UTC)
        rows = await self.ledger.list_news(limit=self.config.scan_batch_size,
                                          after_key=after_key, include_retracted=True)
        for row in rows:
            try:
                if row["status"] == "active":
                    policy = KnowledgeLifecycleConfig.model_validate(row["config"])
                    hold = row.get("hold_until")
                    tier = policy.classify_tier(datetime.fromisoformat(row["publish_time"]), now,
                        reheat_until=datetime.fromisoformat(hold) if hold else None)
                    if tier.value != row["desired_tier"]:
                        await self.ledger.request_migration(tenant_id=row["tenant_id"], news_id=row["news_id"],
                            target_tier=tier.value, expected_generation=row["generation"])
                await self.reconcile(tenant_id=row["tenant_id"], news_id=row["news_id"])
            except Exception as exc:
                # One broken projection/policy must not starve every later news
                # item on every scheduled pass. Persist a redacted fault and
                # revisit the row next pass. Failed error persistence is a page
                # failure, so database outages still reach Temporal retries.
                await self.ledger.record_maintenance_error(tenant_id=row["tenant_id"],
                    job_id=row["current_job_id"], error="Scan" + type(exc).__name__)
                log.error("knowledge lifecycle scan failed tenant=%s news=%s job=%s error=%s",
                    row["tenant_id"], row["news_id"], row["current_job_id"], type(exc).__name__)
        return (rows[-1]["tenant_id"], rows[-1]["news_id"]) if rows else None
