"""Durable, leased semantic work and immutable pair decisions in PostgreSQL.

No model call holds a transaction. The lifecycle ledger remains the serving authority.
"""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import (BigInteger, Column, DateTime, Index, Integer, MetaData,
                        String, Table, and_, insert, or_, select, update)

from app.modules.knowledge.tier_lifecycle_store import _json, _news, _jobs, _public
from app.modules.knowledge.news_semantics_contracts import (
    Features, PairUnderstanding, SemanticConfig, decide_pair, digest, event_identity, validate_features,
)


metadata = MetaData()
analyses = Table("news_semantic_analyses", metadata,
    Column("analysis_id", String(64), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("news_id", String(160), nullable=False),
    Column("source_revision", BigInteger, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("bundle_id", String(64), nullable=False),
    Column("config", _json, nullable=False),
    Column("status", String(16), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("lease_token", String(32)),
    Column("lease_until", DateTime(timezone=True)),
    Column("next_check", DateTime(timezone=True), nullable=False),
    Column("features", _json, nullable=False),
    Column("event_id", String(64)),
    Column("last_error", String(100)),
    Column("updated_at", DateTime(timezone=True), nullable=False))
Index("ix_news_semantic_current", analyses.c.tenant_id, analyses.c.news_id,
      analyses.c.source_revision, analyses.c.bundle_id, unique=True)
Index("ix_news_semantic_event", analyses.c.tenant_id, analyses.c.event_id)

pairs = Table("news_semantic_pairs", metadata,
    Column("decision_id", String(64), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("bundle_id", String(64), nullable=False),
    Column("left_analysis_id", String(64), nullable=False),
    Column("right_analysis_id", String(64), nullable=False),
    Column("left_news_id", String(160), nullable=False),
    Column("right_news_id", String(160), nullable=False),
    Column("result", _json, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False))
Index("ix_news_semantic_pairs_left", pairs.c.tenant_id, pairs.c.left_news_id)
Index("ix_news_semantic_pairs_right", pairs.c.tenant_id, pairs.c.right_news_id)


def analysis_id(row, config):
    return digest([row["tenant_id"], row["news_id"], row["source_revision"],
                   row["content_hash"], config.bundle_id])


def pair_id(left_id, right_id):
    return digest(sorted((left_id, right_id)))


def is_current(analysis, news):
    return bool(analysis and news and analysis["tenant_id"] == news["tenant_id"]
                and analysis["news_id"] == news["news_id"]
                and analysis["source_revision"] == news["source_revision"]
                and analysis["content_hash"] == news["content_hash"]
                and news["status"] in ("active", "deduplicated"))


async def validate_retirement(session, *, tenant_id, decision_id, source, replacement, bundle_id):
    """Called INSIDE the same lifecycle transaction after ordered source/target locks."""
    pair = (await session.execute(select(pairs).where(pairs.c.tenant_id == tenant_id,
        pairs.c.decision_id == decision_id, pairs.c.bundle_id == bundle_id))).mappings().first()
    if not pair:
        raise ValueError("semantic decision not found in the current bundle")
    rows = {r["analysis_id"]: r for r in (await session.execute(select(analyses).where(
        analyses.c.tenant_id == tenant_id, analyses.c.analysis_id.in_(
            [pair["left_analysis_id"], pair["right_analysis_id"]])))).mappings()}
    la, ra = rows.get(pair["left_analysis_id"]), rows.get(pair["right_analysis_id"])
    news = {source["news_id"]: source, replacement["news_id"]: replacement}
    if not la or not ra or not is_current(la, news.get(la["news_id"])) or not is_current(ra, news.get(ra["news_id"])):
        raise ValueError("semantic decision refers to stale content")
    cfg = SemanticConfig.model_validate(la["config"])
    if cfg.bundle_id != bundle_id or ra["bundle_id"] != bundle_id:
        raise ValueError("semantic bundle drift")
    lf, rf = Features.model_validate(la["features"]), Features.model_validate(ra["features"])
    left, right = news[la["news_id"]], news[ra["news_id"]]
    validate_features(lf, left["body"])
    validate_features(rf, right["body"])
    result = decide_pair(left, right, lf, rf,
        PairUnderstanding.model_validate(pair["result"]["understanding"]), cfg)
    if (result != pair["result"] or result["redundant_news_id"] != source["news_id"]
            or result["replacement_news_id"] != replacement["news_id"]):
        raise ValueError("semantic evidence does not authorize this replacement")
    return {"semantic_decision_id": decision_id, "semantic_bundle_id": bundle_id,
            "policy_version": cfg.policy_version}


class NewsSemanticStore:
    def __init__(self, database, ledger, config):
        self.database, self.ledger, self.config = database, ledger, config

    async def validate(self):
        async with self.database.session() as session:
            await session.execute(select(analyses.c.analysis_id).limit(1))
            await session.execute(select(pairs.c.decision_id).limit(1))

    async def get(self, row):
        async with self.database.session() as session:
            value = (await session.execute(select(analyses).where(
                analyses.c.analysis_id == analysis_id(row, self.config)))).mappings().first()
            return _public(value)

    async def claim(self, row):
        now, cfg = datetime.now(UTC), self.config
        async with self.database.session() as session:
            current = await self.ledger._lock(session, row["tenant_id"], row["news_id"])
            if (not current or current["status"] != "active"
                    or current["content_hash"] != row["content_hash"]
                    or current["source_revision"] != row["source_revision"]):
                return None
            key = analysis_id(row, cfg)
            existing = (await session.execute(select(analyses).where(
                analyses.c.analysis_id == key).with_for_update())).mappings().first()
            if existing:
                until = existing["lease_until"]
                if (existing["status"] in ("failed", "skipped")
                        or (until and until.replace(tzinfo=until.tzinfo or UTC) > now)
                        or existing["next_check"].replace(tzinfo=existing["next_check"].tzinfo or UTC) > now):
                    return None
                if existing["status"] != "complete" and existing["attempts"] >= cfg.max_attempts:
                    await session.execute(update(analyses).where(analyses.c.analysis_id == key).values(
                        status="failed", last_error="LeaseAttemptsExhausted", lease_token=None, lease_until=None))
                    return None
            values = dict(status="running", attempts=1 if not existing or existing["status"] == "complete"
                          else existing["attempts"] + 1, lease_token=uuid4().hex,
                          lease_until=now + timedelta(seconds=cfg.lease_seconds), next_check=now, updated_at=now)
            if existing:
                await session.execute(update(analyses).where(analyses.c.analysis_id == key).values(**values))
                return _public({**dict(existing), **values})
            value = dict(analysis_id=key, tenant_id=row["tenant_id"], news_id=row["news_id"],
                source_revision=row["source_revision"], content_hash=row["content_hash"], bundle_id=cfg.bundle_id,
                config=cfg.model_dump(mode="json"), features={}, event_id=None, last_error=None, **values)
            await session.execute(insert(analyses).values(**value))
            return _public(value)

    async def save_features(self, job, row, features):
        validate_features(features, row["body"])
        async with self.database.session() as session:
            current = await self.ledger._lock(session, row["tenant_id"], row["news_id"])
            if not is_current(job, current):
                raise ValueError("source changed during semantic extraction")
            saved = (await session.execute(select(analyses).where(analyses.c.analysis_id == job["analysis_id"],
                analyses.c.lease_token == job["lease_token"],
                analyses.c.lease_until > datetime.now(UTC)))).mappings().first()
            if not saved:
                raise ValueError("semantic lease was superseded")
            if saved["features"]:
                return _public(saved)  # First validated extraction wins for this immutable bundle/revision.
            value = dict(features=features.model_dump(mode="json"),
                         event_id=event_identity(row["tenant_id"], features, row["news_id"], row["source_revision"]))
            result = await session.execute(update(analyses).where(
                analyses.c.analysis_id == job["analysis_id"], analyses.c.lease_token == job["lease_token"],
                analyses.c.lease_until > datetime.now(UTC)).values(**value))
            if result.rowcount != 1:
                raise ValueError("semantic lease was superseded")
            return {**job, **value}

    async def finish(self, job, *, error=None, skipped=False):
        now = datetime.now(UTC)
        status = ("skipped" if skipped else "failed" if error and job["attempts"] >= self.config.max_attempts
                  else "retry" if error else "complete")
        async with self.database.session() as session:
            await session.execute(update(analyses).where(analyses.c.analysis_id == job["analysis_id"],
                analyses.c.lease_token == job["lease_token"], analyses.c.lease_until > now).values(
                    status=status, last_error=error, lease_token=None, lease_until=None,
                    next_check=now + timedelta(seconds=self.config.retry_seconds if error else self.config.refresh_seconds), updated_at=now))

    async def retry(self, row):
        async with self.database.session() as session:
            await session.execute(update(analyses).where(analyses.c.analysis_id == analysis_id(row, self.config),
                analyses.c.status.in_(("failed", "retry", "complete"))).values(
                    status="retry", attempts=0, next_check=datetime.now(UTC), last_error=None))

    async def get_pair(self, left_id, right_id):
        async with self.database.session() as session:
            value = (await session.execute(select(pairs).where(pairs.c.decision_id == pair_id(left_id, right_id),
                pairs.c.bundle_id == self.config.bundle_id
            ))).mappings().first()
            return _public(value)

    async def decision(self, tenant_id, decision_id):
        async with self.database.session() as session:
            return _public((await session.execute(select(pairs).where(pairs.c.tenant_id == tenant_id,
                pairs.c.decision_id == decision_id, pairs.c.bundle_id == self.config.bundle_id))).mappings().first())

    async def save_pair(self, left, right, la, ra, understanding):
        if left["tenant_id"] != right["tenant_id"] or left["news_id"] == right["news_id"]:
            raise ValueError("invalid semantic pair scope")
        key = pair_id(la["analysis_id"], ra["analysis_id"])
        async with self.database.session() as session:
            locked = {n: await self.ledger._lock(session, left["tenant_id"], n)
                      for n in sorted((left["news_id"], right["news_id"]))}
            if (not is_current(la, locked[left["news_id"]]) or not is_current(ra, locked[right["news_id"]])
                    or any(r["status"] != "active" for r in locked.values())):
                raise ValueError("news changed during semantic comparison")
            owner = (await session.execute(select(analyses).where(analyses.c.analysis_id == la["analysis_id"])))
            owner = owner.mappings().first()
            if (not owner or owner["lease_token"] != la["lease_token"] or not owner["lease_until"]
                    or owner["lease_until"].replace(tzinfo=owner["lease_until"].tzinfo or UTC) <= datetime.now(UTC)):
                raise ValueError("semantic comparison lease was superseded")
            partner = (await session.execute(select(analyses).where(
                analyses.c.analysis_id == ra["analysis_id"], analyses.c.tenant_id == right["tenant_id"],
                analyses.c.bundle_id == self.config.bundle_id))).mappings().first()
            if (not partner or owner["bundle_id"] != self.config.bundle_id
                    or owner["features"] != la["features"] or partner["features"] != ra["features"]):
                raise ValueError("semantic comparison features changed")
            existing = (await session.execute(select(pairs).where(pairs.c.decision_id == key))).mappings().first()
            if existing:
                return _public(existing)
            result = decide_pair(left, right, Features.model_validate(la["features"]),
                                 Features.model_validate(ra["features"]), understanding, self.config)
            value = dict(decision_id=key, tenant_id=left["tenant_id"], bundle_id=self.config.bundle_id,
                left_analysis_id=la["analysis_id"], right_analysis_id=ra["analysis_id"],
                left_news_id=left["news_id"], right_news_id=right["news_id"], result=result, created_at=datetime.now(UTC))
            await session.execute(insert(pairs).values(**value))
            return _public(value)

    async def live_pairs(self, tenant_id, news_ids, *, include_retired=False, limit=200):
        if not news_ids:
            return []
        la, ra = analyses.alias("la"), analyses.alias("ra")
        ln, rn = _news.alias("ln"), _news.alias("rn")
        statuses = ("active", "deduplicated") if include_retired else ("active",)
        def live(a, n):
            return and_(a.c.tenant_id == n.c.tenant_id, a.c.news_id == n.c.news_id,
                a.c.source_revision == n.c.source_revision, a.c.content_hash == n.c.content_hash,
                n.c.status.in_(statuses))
        query = select(pairs).select_from(pairs.join(la, pairs.c.left_analysis_id == la.c.analysis_id)
            .join(ra, pairs.c.right_analysis_id == ra.c.analysis_id).join(ln, live(la, ln)).join(rn, live(ra, rn)))
        query = query.where(pairs.c.tenant_id == tenant_id, pairs.c.bundle_id == self.config.bundle_id,
            or_(pairs.c.left_news_id.in_(news_ids), pairs.c.right_news_id.in_(news_ids)))
        async with self.database.session() as session:
            return [_public(r) for r in (await session.execute(
                query.order_by(pairs.c.decision_id).limit(limit))).mappings()]

    async def projection(self, tenant_id, news_id):
        async with self.database.session() as session:
            row = (await session.execute(select(_jobs).select_from(_jobs.join(_news,
                and_(_news.c.tenant_id == _jobs.c.tenant_id, _news.c.active_job_id == _jobs.c.job_id))).where(
                    _news.c.tenant_id == tenant_id, _news.c.news_id == news_id,
                    _news.c.status == "active"))).mappings().first()
            return _public(row)

    async def evidence_quotes(self, pair, news_id):
        side = "left" if news_id == pair["left_news_id"] else "right"
        async with self.database.session() as session:
            row = (await session.execute(select(analyses.c.features).where(
                analyses.c.tenant_id == pair["tenant_id"],
                analyses.c.analysis_id == pair[f"{side}_analysis_id"]))).first()
        if not row:
            return []
        result = pair["result"]
        wanted = (result[f"only_{side}"] if result["relation"] == "same_event"
                  else result["understanding"][f"evidence_{side}"])
        return [q["text"] for c in row[0]["claims"] if c["id"] in wanted for q in c["evidence"]]
