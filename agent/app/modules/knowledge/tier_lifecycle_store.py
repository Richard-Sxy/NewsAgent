"""Durable news/projection ledger for the opt-in three-tier indexing pipeline.

PostgreSQL is the serving authority. Vector databases are replaceable projections;
an obsolete worker can write a projection, but cannot activate it. All external
model/vector calls belong outside these short database transactions.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
import json
import math
from uuid import uuid4

from sqlalchemy import (
    JSON, BigInteger, Boolean, Column, DateTime, Index, Integer, MetaData, String, Table,
    Text, UniqueConstraint, and_, insert, or_, select, text, update,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.db.session import Database
from app.modules.knowledge.duplicate_policy import duplicate_evidence
from app.retrieval.tiered_vector import VectorSearchHit, VectorTier


_metadata = MetaData()
_json = JSON().with_variant(JSONB(), "postgresql")
_news = Table(
    "tier_lifecycle_news", _metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("news_id", String(160), primary_key=True),
    Column("document_id", String(160), nullable=False),
    Column("source_revision", BigInteger, nullable=False),
    Column("generation", BigInteger, nullable=False),
    Column("status", String(16), nullable=False),
    Column("event_hash", String(64), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("title", String(500), nullable=False),
    Column("body", Text, nullable=False),
    Column("publish_time", DateTime(timezone=True)),
    Column("source_url", Text),
    Column("desired_tier", String(16), nullable=False),
    Column("active_tier", String(16)),
    Column("active_job_id", String(32)),
    Column("current_job_id", String(32), nullable=False),
    Column("accepted_job_id", String(32), nullable=False),
    Column("config", _json, nullable=False),
    Column("hold_until", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("tenant_id", "document_id", name="uq_tier_lifecycle_news_document"),
)
_jobs = Table(
    "tier_lifecycle_jobs", _metadata,
    Column("job_id", String(32), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("news_id", String(160), nullable=False),
    Column("source_revision", BigInteger, nullable=False),
    Column("generation", BigInteger, nullable=False),
    Column("kind", String(16), nullable=False),
    Column("target_tier", String(16), nullable=False),
    Column("config", _json, nullable=False),
    Column("payload", _json, nullable=False),
    Column("records", _json, nullable=False),
    Column("chunk_ids", _json, nullable=False),
    Column("phase", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("cleanup_dirty", Boolean, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("last_error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
Index("ix_tier_lifecycle_jobs_news", _jobs.c.tenant_id, _jobs.c.news_id)
Index("ix_tier_lifecycle_jobs_pending", _jobs.c.status, _jobs.c.job_id)

# Isolated, durable local Port substitute. Production deployments use Milvus.
_vectors = Table(
    "tier_lifecycle_vectors", _metadata,
    Column("chunk_id", String(64), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("tier", String(16), nullable=False),
    Column("record", _json, nullable=False),
)
Index("ix_tier_lifecycle_vectors_scope", _vectors.c.tenant_id, _vectors.c.tier)


def projection_chunk_id(job_id: str, chunk_index: int) -> str:
    """A projection owns its IDs forever, including after return migration."""
    return sha256(f"{job_id}:{chunk_index}".encode()).hexdigest()


def _canonical(value):
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                allow_nan=False, default=_datetime_json))


def _datetime_json(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include timezone")
        return value.astimezone(UTC).isoformat()
    raise TypeError(f"unsupported JSON type: {type(value).__name__}")


def _public(row):
    if row is None:
        return None
    result = dict(row)
    # SQLite test databases discard timezone information. Production columns are
    # timestamptz; normalize returned fields to one explicit wire representation.
    for key, value in result.items():
        if isinstance(value, datetime):
            result[key] = value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).isoformat()
    return result


def _digest(value) -> str:
    return sha256(json.dumps(_canonical(value), ensure_ascii=False,
                             sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _scope(tenant_id: str, news_id: str):
    return and_(_news.c.tenant_id == tenant_id, _news.c.news_id == news_id)


def _identity(tenant_id: str, news_id: str, source_revision: int | None = None):
    if not tenant_id.strip() or len(tenant_id) > 128 or not news_id.strip() or len(news_id) > 160:
        raise ValueError("invalid tenant or news identity")
    if source_revision is not None and (not isinstance(source_revision, int)
                                       or isinstance(source_revision, bool)
                                       or not 1 <= source_revision <= 2**63 - 1):
        raise ValueError("source_revision must be a positive signed 64-bit integer")


class TierLifecycleStore:
    def __init__(self, *, database: Database) -> None:
        self._database = database

    async def _lock(self, session, tenant_id: str, news_id: str):
        if self._database.engine.dialect.name == "postgresql":
            key = int.from_bytes(sha256(f"{tenant_id}\0{news_id}".encode()).digest()[:8],
                                 "big", signed=True)
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        return (await session.execute(select(_news).where(_scope(tenant_id, news_id))
                                      .with_for_update())).mappings().first()

    async def _job(self, session, tenant_id: str, job_id: str, *, locked=False):
        statement = select(_jobs).where(_jobs.c.tenant_id == tenant_id, _jobs.c.job_id == job_id)
        if locked:
            statement = statement.with_for_update()
        return (await session.execute(statement)).mappings().first()

    async def _dirty_old(self, session, tenant_id, news_id, current_job_id):
        await session.execute(update(_jobs).where(
            _jobs.c.tenant_id == tenant_id, _jobs.c.news_id == news_id,
            _jobs.c.job_id != current_job_id,
        ).values(cleanup_dirty=True, updated_at=datetime.now(UTC)))

    def _new_job(self, *, tenant_id, news_id, source_revision, generation, kind,
                 target_tier, config, payload):
        now = datetime.now(UTC)
        return dict(job_id=uuid4().hex, tenant_id=tenant_id, news_id=news_id,
                    source_revision=source_revision, generation=generation, kind=kind,
                    target_tier=str(VectorTier(target_tier)), config=_canonical(config),
                    payload=_canonical(payload), records=[], chunk_ids=[], phase="pending", status="pending",
                    cleanup_dirty=True, attempts=0, last_error=None,
                    created_at=now, updated_at=now)

    async def accept_content(self, *, tenant_id: str, news_id: str, source_revision: int,
                             title: str, body: str, publish_time: datetime,
                             source_url: str | None, desired_tier: str, config: dict,
                             document_id: str | None = None) -> dict:
        _identity(tenant_id, news_id, source_revision)
        document_id = document_id if document_id is not None else news_id
        if not document_id.strip() or len(document_id) > 160:
            raise ValueError("invalid document identity")
        if not title.strip() or len(title) > 500 or not body.strip() or len(body) > 1_000_000:
            raise ValueError("invalid title or body length")
        if not config.get("embedding_version") or not config.get("chunking_version"):
            raise ValueError("embedding_version and chunking_version are required")
        payload = _canonical(dict(title=title, body=body, publish_time=publish_time,
                                  source_url=source_url))
        content_hash = _digest(payload)
        event_hash = _digest({"kind": "upsert", "content": payload})
        desired_tier = str(VectorTier(desired_tier))
        async with self._database.session() as session:
            current = await self._lock(session, tenant_id, news_id)
            if current and current["document_id"] != document_id and current["content_hash"]:
                raise ValueError("news document identity cannot change")
            if current and source_revision <= current["source_revision"]:
                if source_revision < current["source_revision"]:
                    raise ValueError("source_revision is older than the accepted revision")
                if current["event_hash"] != event_hash:
                    raise ValueError("same source_revision has conflicting content or operation")
                return _public(await self._job(session, tenant_id, current["accepted_job_id"]))
            generation = current["generation"] + 1 if current else 1
            job = self._new_job(tenant_id=tenant_id, news_id=news_id,
                                source_revision=source_revision, generation=generation,
                                kind="upsert", target_tier=desired_tier, config=config, payload=payload)
            await session.execute(insert(_jobs).values(**job))
            values = dict(tenant_id=tenant_id, news_id=news_id, document_id=document_id,
                          source_revision=source_revision,
                          generation=generation, status="pending", event_hash=event_hash,
                          content_hash=content_hash, title=title, body=body,
                          publish_time=publish_time.astimezone(UTC), source_url=source_url,
                          desired_tier=desired_tier, active_tier=None, active_job_id=None,
                          current_job_id=job["job_id"], accepted_job_id=job["job_id"],
                          config=_canonical(config), hold_until=None, updated_at=datetime.now(UTC))
            if current:
                await session.execute(update(_news).where(_scope(tenant_id, news_id)).values(**values))
            else:
                await session.execute(insert(_news).values(**values))
            await self._dirty_old(session, tenant_id, news_id, job["job_id"])
            return _public(job)

    async def retract(self, *, tenant_id: str, news_id: str, source_revision: int) -> dict:
        _identity(tenant_id, news_id, source_revision)
        event_hash = _digest({"kind": "retract"})
        async with self._database.session() as session:
            current = await self._lock(session, tenant_id, news_id)
            if current and source_revision <= current["source_revision"]:
                if source_revision < current["source_revision"]:
                    raise ValueError("source_revision is older than the accepted revision")
                if current["event_hash"] != event_hash:
                    raise ValueError("same source_revision has conflicting content or operation")
                return _public(await self._job(session, tenant_id, current["accepted_job_id"]))
            generation = current["generation"] + 1 if current else 1
            job = self._new_job(tenant_id=tenant_id, news_id=news_id, source_revision=source_revision,
                                generation=generation, kind="retract",
                                target_tier=current["desired_tier"] if current else "hot",
                                config=current["config"] if current else {}, payload={})
            await session.execute(insert(_jobs).values(**job))
            values = dict(source_revision=source_revision, generation=generation, status="retracted",
                          event_hash=event_hash, active_job_id=None, active_tier=None,
                          current_job_id=job["job_id"], accepted_job_id=job["job_id"],
                          hold_until=None, updated_at=datetime.now(UTC))
            if current:
                await session.execute(update(_news).where(_scope(tenant_id, news_id)).values(**values))
            else:
                await session.execute(insert(_news).values(tenant_id=tenant_id, news_id=news_id,
                    document_id=news_id, content_hash="", title="", body="", publish_time=None, source_url=None,
                    desired_tier="hot", config={}, **values))
            await self._dirty_old(session, tenant_id, news_id, job["job_id"])
            return _public(job)

    async def retire_duplicate(self, *, tenant_id: str, news_id: str, source_revision: int,
                               source_job_id: str, replacement_news_id: str,
                               replacement_revision: int, replacement_job_id: str,
                               semantic_decision_id: str | None = None,
                               semantic_bundle_id: str | None = None) -> dict:
        """Retire one verified duplicate projection, preserving its source content.

        The service verifies the replacement in the vector backend first. Here
        ordered row locks recheck both exact versions and the strict policy;
        retirement and durable cleanup intent commit together. No source
        revision is invented and no source news record is deleted.
        """
        _identity(tenant_id, news_id, source_revision)
        _identity(tenant_id, replacement_news_id, replacement_revision)
        if news_id == replacement_news_id:
            raise ValueError("replacement must be a different news item")
        request = dict(source_job_id=source_job_id, replacement_news_id=replacement_news_id,
                       replacement_revision=replacement_revision, replacement_job_id=replacement_job_id)
        if bool(semantic_decision_id) != bool(semantic_bundle_id):
            raise ValueError("semantic decision and bundle are both required")
        if semantic_decision_id:
            request.update(semantic_decision_id=semantic_decision_id, semantic_bundle_id=semantic_bundle_id)
        async with self._database.session() as session:
            rows = {key: await self._lock(session, tenant_id, key)
                    for key in sorted((news_id, replacement_news_id))}
            current, replacement = rows[news_id], rows[replacement_news_id]
            if not current or current["source_revision"] != source_revision:
                raise ValueError("source revision is not current")
            if current["status"] == "deduplicated":
                previous = await self._job(session, tenant_id, current["current_job_id"])
                if all(previous["payload"].get(key) == value for key, value in request.items()):
                    return _public(previous)
                raise ValueError("source was already retired by a different decision")
            for row, expected_revision, expected_job in (
                (current, source_revision, source_job_id),
                (replacement, replacement_revision, replacement_job_id),
            ):
                if (not row or row["status"] != "active"
                        or row["source_revision"] != expected_revision
                        or row["active_job_id"] != expected_job
                        or row["current_job_id"] != expected_job):
                    raise ValueError("both exact projections must remain active with no pending change")
            # Do not create A -> B -> C replacement chains. A target cannot itself
            # retire while retained sources depend on it; ordered news locks serialize this.
            dependent = (await session.execute(select(_jobs.c.job_id).select_from(_jobs.join(_news,
                and_(_news.c.tenant_id == _jobs.c.tenant_id, _news.c.current_job_id == _jobs.c.job_id))).where(
                    _news.c.tenant_id == tenant_id, _news.c.status == "deduplicated",
                    _jobs.c.payload["replacement_news_id"].as_string() == news_id).limit(1))).first()
            if dependent:
                raise ValueError("source is still the replacement for retired news")
            if semantic_decision_id:
                from app.modules.knowledge.news_semantics_store import validate_retirement
                evidence = await validate_retirement(session, tenant_id=tenant_id,
                    decision_id=semantic_decision_id, source=current, replacement=replacement,
                    bundle_id=semantic_bundle_id)
            else:
                evidence = duplicate_evidence(current, replacement)
            job = self._new_job(tenant_id=tenant_id, news_id=news_id,
                source_revision=source_revision, generation=current["generation"] + 1,
                kind="deduplicate", target_tier=current["desired_tier"], config=current["config"],
                payload={**request, **evidence})
            await session.execute(insert(_jobs).values(**job))
            # Keep active_job_id as the source-content provenance. Vector gates
            # require status=active, and obsolete_jobs retires all its old IDs.
            await session.execute(update(_news).where(_scope(tenant_id, news_id)).values(
                status="deduplicated", generation=job["generation"], current_job_id=job["job_id"],
                hold_until=None, updated_at=datetime.now(UTC)))
            await self._dirty_old(session, tenant_id, news_id, job["job_id"])
            return _public(job)

    async def restore_duplicate(self, *, tenant_id: str, news_id: str,
                                retirement_job_id: str, target_tier: str) -> dict:
        """Explicitly rebuild the retained source using fresh, non-reusable IDs."""
        _identity(tenant_id, news_id)
        async with self._database.session() as session:
            current = await self._lock(session, tenant_id, news_id)
            retired = await self._job(session, tenant_id, retirement_job_id)
            if (not current or not retired or retired["news_id"] != news_id
                    or retired["kind"] != "deduplicate"
                    or retired["source_revision"] != current["source_revision"]):
                raise ValueError("retirement does not match the current source revision")
            # A lost HTTP response may replay a successful restore submission.
            if current["generation"] == retired["generation"] + 1:
                job = await self._job(session, tenant_id, current["current_job_id"])
                if job["kind"] == "upsert":
                    return _public(job)
            current_job = await self._job(session, tenant_id, current["current_job_id"])
            if (current["status"] != "deduplicated" or current_job["kind"] != "deduplicate"
                    or current_job["job_id"] != retirement_job_id):
                raise ValueError("only the current duplicate retirement can be restored")
            job = self._new_job(tenant_id=tenant_id, news_id=news_id,
                source_revision=current["source_revision"], generation=current["generation"] + 1,
                kind="upsert", target_tier=target_tier, config=current["config"],
                payload={key: _public(current)[key] for key in
                         ("title", "body", "publish_time", "source_url")})
            await session.execute(insert(_jobs).values(**job))
            await session.execute(update(_news).where(_scope(tenant_id, news_id)).values(
                status="pending", generation=job["generation"], desired_tier=job["target_tier"],
                active_job_id=None, active_tier=None, current_job_id=job["job_id"],
                accepted_job_id=job["job_id"], updated_at=datetime.now(UTC)))
            await self._dirty_old(session, tenant_id, news_id, job["job_id"])
            return _public(job)

    async def request_migration(self, *, tenant_id: str, news_id: str, target_tier: str,
                                config: dict | None = None, expected_generation: int | None = None,
                                hold_until: datetime | None = None) -> dict | None:
        _identity(tenant_id, news_id)
        target_tier = str(VectorTier(target_tier))
        if hold_until is not None:
            _datetime_json(hold_until)
        async with self._database.session() as session:
            current = await self._lock(session, tenant_id, news_id)
            if not current or current["status"] in ("retracted", "deduplicated"):
                return None
            if expected_generation is not None and current["generation"] != expected_generation:
                return None
            frozen = _canonical(config if config is not None else current["config"])
            same_hold = (_public(current)["hold_until"] ==
                         (_datetime_json(hold_until) if hold_until else None))
            if current["desired_tier"] == target_tier and current["config"] == frozen and same_hold:
                return _public(await self._job(session, tenant_id, current["current_job_id"]))
            if not frozen.get("embedding_version") or not frozen.get("chunking_version"):
                raise ValueError("embedding_version and chunking_version are required")
            job = self._new_job(tenant_id=tenant_id, news_id=news_id,
                                source_revision=current["source_revision"],
                                generation=current["generation"] + 1, kind="migrate",
                                target_tier=target_tier, config=frozen,
                                payload={key: _public(current)[key] for key in
                                         ("title", "body", "publish_time", "source_url")})
            await session.execute(insert(_jobs).values(**job))
            await session.execute(update(_news).where(_scope(tenant_id, news_id)).values(
                generation=job["generation"], desired_tier=target_tier,
                current_job_id=job["job_id"], config=frozen, hold_until=hold_until,
                updated_at=datetime.now(UTC)))
            return _public(job)

    async def get_job(self, *, tenant_id: str, job_id: str) -> dict | None:
        async with self._database.session() as session:
            return _public(await self._job(session, tenant_id, job_id))

    async def reusable_source_job(self, *, tenant_id: str, job_id: str) -> dict | None:
        """Snapshot only the current migration's still-active source projection.

        This is an optimization candidate, never activation authority. The
        service validates its complete chunk/vector contract, and activate()
        must still fence corrections, withdrawals and newer migrations that
        arrive after this short transaction ends. No external calls run here.
        """
        async with self._database.session() as session:
            target = await self._job(session, tenant_id, job_id)
            if target is None or target["kind"] != "migrate":
                return None
            current = await self._lock(session, tenant_id, target["news_id"])
            target = await self._job(session, tenant_id, job_id)
            if (not current or current["status"] != "active"
                    or current["current_job_id"] != job_id
                    or current["generation"] != target["generation"]
                    or current["source_revision"] != target["source_revision"]
                    or target["status"] == "failed"
                    or not current["active_job_id"] or current["active_job_id"] == job_id):
                return None
            source = await self._job(session, tenant_id, current["active_job_id"])
            if (not source or source["news_id"] != target["news_id"]
                    or source["kind"] == "retract"
                    or source["source_revision"] != target["source_revision"]
                    or source["generation"] >= target["generation"]
                    or source["target_tier"] != current["active_tier"]
                    or source["phase"] not in ("active", "complete")
                    or source["payload"] != target["payload"]
                    or _digest(target["payload"]) != current["content_hash"]):
                return None
            return _public(source)

    async def get_news(self, *, tenant_id: str, news_id: str) -> dict | None:
        async with self._database.session() as session:
            row = (await session.execute(select(_news).where(_scope(tenant_id, news_id)))).mappings().first()
            return _public(row)

    async def get_document(self, *, tenant_id: str, document_id: str) -> dict | None:
        async with self._database.session() as session:
            row = (await session.execute(select(_news).where(_news.c.tenant_id == tenant_id,
                _news.c.document_id == document_id))).mappings().first()
            return _public(row)

    async def list_pending(self, *, limit: int = 100, after_job_id: str | None = None) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be in [1, 1000]")
        # Dispatch needs routing/status metadata, never article text or vectors.
        # A saved projection can contain up to a million coordinates; fetching
        # a whole page of them here would multiply the per-document memory bound.
        query = select(*(_jobs.c[name] for name in (
            "job_id", "tenant_id", "news_id", "source_revision", "generation",
            "kind", "target_tier", "config", "phase", "status", "cleanup_dirty",
            "attempts", "last_error", "created_at", "updated_at",
        ))).where(or_(
            _jobs.c.status.in_(("pending", "retry")),
            and_(_jobs.c.cleanup_dirty.is_(True), _jobs.c.status != "failed"),
        ))
        if after_job_id:
            query = query.where(_jobs.c.job_id > after_job_id)
        async with self._database.session() as session:
            return [_public(row) for row in (await session.execute(
                query.order_by(_jobs.c.job_id).limit(limit))).mappings()]

    async def list_news(self, *, limit: int = 100,
                        after_key: tuple[str, str] | None = None,
                        include_retracted: bool = False) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be in [1, 1000]")
        query = select(_news)
        if not include_retracted:
            query = query.where(_news.c.status.in_(("active", "pending")))
        if after_key:
            tenant, news = after_key
            query = query.where(or_(_news.c.tenant_id > tenant,
                and_(_news.c.tenant_id == tenant, _news.c.news_id > news)))
        async with self._database.session() as session:
            return [_public(row) for row in (await session.execute(query.order_by(
                _news.c.tenant_id, _news.c.news_id).limit(limit))).mappings()]

    async def save_records(self, *, tenant_id: str, job_id: str, records: list[dict]) -> dict:
        canonical = _canonical(records)
        async with self._database.session() as session:
            job = await self._job(session, tenant_id, job_id, locked=True)
            if job is None:
                raise ValueError("job not found")
            if job["records"]:
                return _public(job)
            if job["kind"] in ("retract", "deduplicate") or not canonical:
                raise ValueError("only content projections accept nonempty records")
            dimension = None
            for index, record in enumerate(canonical):
                if (record.get("chunk_id") != projection_chunk_id(job_id, index)
                    or record.get("chunk_index") != index
                    or record.get("tenant_id") != tenant_id
                    or record.get("news_id") != job["news_id"]
                    or record.get("content_version") != job["source_revision"]
                    or record.get("embedding_version") != job["config"]["embedding_version"]):
                    raise ValueError("record identity does not match immutable job projection")
                vector = record.get("vector", [])
                if not vector or not all(isinstance(v, (float, int)) and not isinstance(v, bool)
                                         and math.isfinite(v) for v in vector):
                    raise ValueError("records require finite, nonempty vectors")
                dimension = dimension or len(vector)
                if len(vector) != dimension:
                    raise ValueError("record vector dimensions differ")
            now = datetime.now(UTC)
            chunk_ids = [record["chunk_id"] for record in canonical]
            await session.execute(update(_jobs).where(_jobs.c.job_id == job_id).values(
                records=canonical, chunk_ids=chunk_ids, phase="embedded", cleanup_dirty=True, updated_at=now))
            return _public({**dict(job), "records": canonical, "phase": "embedded",
                            "chunk_ids": chunk_ids, "cleanup_dirty": True, "updated_at": now})

    async def activate(self, *, tenant_id: str, job_id: str) -> bool:
        async with self._database.session() as session:
            job = await self._job(session, tenant_id, job_id)
            if job is None:
                raise ValueError("job not found")
            current = await self._lock(session, tenant_id, job["news_id"])
            job = await self._job(session, tenant_id, job_id, locked=True)
            if (not current or current["status"] in ("retracted", "deduplicated")
                or job["kind"] in ("retract", "deduplicate")
                or job["status"] == "failed"
                or current["generation"] != job["generation"]
                or current["current_job_id"] != job_id or not job["records"]):
                return False
            now = datetime.now(UTC)
            await session.execute(update(_news).where(_scope(tenant_id, job["news_id"])).values(
                status="active", active_job_id=job_id, active_tier=job["target_tier"], updated_at=now))
            # Replayed activation never regresses a completed job to pending.
            if job["phase"] != "complete":
                await session.execute(update(_jobs).where(_jobs.c.job_id == job_id).values(
                    phase="active", status="pending", last_error=None, updated_at=now))
            await self._dirty_old(session, tenant_id, job["news_id"], job_id)
            return True

    async def complete(self, *, tenant_id: str, job_id: str) -> None:
        async with self._database.session() as session:
            await session.execute(update(_jobs).where(_jobs.c.tenant_id == tenant_id,
                _jobs.c.job_id == job_id, _jobs.c.status != "failed").values(phase="complete", status="complete",
                    cleanup_dirty=False, last_error=None, updated_at=datetime.now(UTC)))

    async def record_error(self, *, tenant_id: str, job_id: str, error: str,
                            retryable: bool = True) -> None:
        async with self._database.session() as session:
            await session.execute(update(_jobs).where(_jobs.c.tenant_id == tenant_id,
                _jobs.c.job_id == job_id, _jobs.c.status.not_in(("complete", "failed"))).values(
                    status="retry" if retryable else "failed", attempts=_jobs.c.attempts + 1,
                    last_error=error[:2000], updated_at=datetime.now(UTC)))

    async def cleanup_complete(self, *, tenant_id: str, job_id: str) -> None:
        await self.complete(tenant_id=tenant_id, job_id=job_id)
        # A failed indexing job stays failed, but verified cleanup must not
        # remain falsely marked dirty forever.
        async with self._database.session() as session:
            await session.execute(update(_jobs).where(_jobs.c.tenant_id == tenant_id,
                _jobs.c.job_id == job_id, _jobs.c.status == "failed").values(
                    cleanup_dirty=False, updated_at=datetime.now(UTC)))

    async def record_maintenance_error(self, *, tenant_id: str, job_id: str,
                                       error: str, cleanup_dirty: bool = False) -> None:
        """Keep scan/cleanup faults inspectable without reopening a terminal job.

        Callers pass an operation name plus exception class, never SDK messages.
        Periodic reconciliation retries physical cleanup; a valid active
        projection remains usable while a different projection cannot be deleted.
        """
        values = dict(last_error=error[:2000], updated_at=datetime.now(UTC))
        if cleanup_dirty:
            values["cleanup_dirty"] = True
        async with self._database.session() as session:
            await session.execute(update(_jobs).where(_jobs.c.tenant_id == tenant_id,
                _jobs.c.job_id == job_id).values(**values))

    async def retry_job(self, *, tenant_id: str, job_id: str) -> dict:
        """Explicitly renew an exhausted current job with a fresh projection fence."""
        async with self._database.session() as session:
            old = await self._job(session, tenant_id, job_id)
            if old is None:
                raise ValueError("job not found")
            current = await self._lock(session, tenant_id, old["news_id"])
            # Read status after the news lock so two retry requests cannot both win.
            old = await self._job(session, tenant_id, job_id, locked=True)
            if (not current or current["current_job_id"] != job_id
                or current["generation"] != old["generation"] or old["status"] != "failed"):
                raise ValueError("only the current failed job can be retried")
            job = self._new_job(tenant_id=tenant_id, news_id=old["news_id"],
                                source_revision=old["source_revision"],
                                generation=current["generation"] + 1, kind=old["kind"],
                                target_tier=old["target_tier"], config=old["config"],
                                payload=old["payload"])
            await session.execute(insert(_jobs).values(**job))
            values = dict(generation=job["generation"], current_job_id=job["job_id"],
                          updated_at=datetime.now(UTC))
            if current["accepted_job_id"] == job_id:
                values["accepted_job_id"] = job["job_id"]
            await session.execute(update(_news).where(_scope(tenant_id, old["news_id"])).values(**values))
            await self._dirty_old(session, tenant_id, old["news_id"], job["job_id"])
            return _public(job)

    async def obsolete_jobs(self, *, tenant_id: str, news_id: str,
                             exclude_job_id: str | None = None) -> list[dict]:
        async with self._database.session() as session:
            current = (await session.execute(select(_news).where(_scope(tenant_id, news_id)))).mappings().first()
            if not current:
                return []
            query = select(_jobs).where(_jobs.c.tenant_id == tenant_id, _jobs.c.news_id == news_id)
            # Never delete the ready predecessor while a replacement is indexing.
            active = current["active_job_id"] if current["status"] != "deduplicated" else None
            excluded = {value for value in (active, current["current_job_id"],
                                             exclude_job_id) if value}
            if excluded:
                query = query.where(_jobs.c.job_id.not_in(excluded))
            return [_public(row) for row in (await session.execute(query.order_by(_jobs.c.job_id))).mappings()
                    if row["records"]]

    async def filter_hits(self, *, tenant_id: str, hits: Sequence[VectorSearchHit]) -> list[VectorSearchHit]:
        if not hits:
            return []
        identities = list({hit.news_id for hit in hits})
        gates = {}
        async with self._database.session() as session:
            for start in range(0, len(identities), 500):
                query = select(_news.c.news_id, _news.c.source_revision, _news.c.active_tier,
                               _jobs.c.config, _jobs.c.chunk_ids).select_from(_news.join(_jobs,
                    and_(_news.c.active_job_id == _jobs.c.job_id,
                         _news.c.tenant_id == _jobs.c.tenant_id))).where(
                    _news.c.tenant_id == tenant_id, _news.c.status == "active",
                    _news.c.news_id.in_(identities[start:start + 500]))
                for row in (await session.execute(query)).mappings():
                    gates[row["news_id"]] = (row["source_revision"], row["active_tier"],
                        row["config"]["embedding_version"], set(row["chunk_ids"]))
        return [hit for hit in hits if hit.news_id in gates
                and hit.content_version == gates[hit.news_id][0]
                and str(hit.tier) == gates[hit.news_id][1]
                and hit.embedding_version == gates[hit.news_id][2]
                and hit.chunk_id in gates[hit.news_id][3]]
