"""Incremental semantics: independent of index readiness, with durable retry state."""
import asyncio
from datetime import datetime
import logging

from app.modules.knowledge.news_semantics_contracts import Features
from app.retrieval.tiered_vector import VectorSearchHit, VectorTier

log = logging.getLogger(__name__)


class NewsSemanticService:
    def __init__(self, *, store, model, lifecycle, query, config):
        self.store, self.model, self.lifecycle, self.query, self.config = store, model, lifecycle, query, config

    async def process(self, row):
        job = await self.store.claim(row)
        if not job:
            return
        if len(row["body"]) > self.config.max_body_chars:
            await self.store.finish(job, error="BodyBudgetExceeded", skipped=True)
            return
        try:
            async with asyncio.timeout(self.config.task_timeout_seconds):
                if not job["features"]:
                    job = await self.store.save_features(job, row, await self.model.extract(row))
                lf = Features.model_validate(job["features"])
                candidates = await self.query.search(tenant_id=row["tenant_id"],
                    query_text=row["title"] + "\n" + row["body"][:1000],
                    limit=self.config.candidate_limit, exclude_news_ids=(row["news_id"],), use_semantics=False)
                for hit in candidates["hits"]:
                    right = await self.lifecycle.ledger.get_news(tenant_id=row["tenant_id"], news_id=hit.news_id)
                    if not right or right["status"] != "active":
                        continue
                    ra = await self.store.get(right)
                    # The other item will be picked up by this same durable scanner.
                    # Periodic discovery also handles two concurrent first arrivals.
                    if not ra or not ra["features"]:
                        continue
                    pair = await self.store.get_pair(job["analysis_id"], ra["analysis_id"])
                    if pair is None:
                        understanding = await self.model.compare(row, right, lf, Features.model_validate(ra["features"]))
                        pair = await self.store.save_pair(row, right, job, ra, understanding)
                    if self.config.auto_retire and pair["result"]["redundant_news_id"]:
                        await self.retire(tenant_id=row["tenant_id"], decision_id=pair["decision_id"])
                await self.store.finish(job)
        except asyncio.CancelledError:
            # Lease expiry makes cancellation/restart recoverable; no stale attempt can publish features.
            raise
        except Exception as exc:
            await self.store.finish(job, error=type(exc).__name__)
            log.warning("news semantics deferred error=%s", type(exc).__name__)

    async def retire(self, *, tenant_id, decision_id):
        pair = await self.store.decision(tenant_id, decision_id)
        if not pair or not pair["result"]["redundant_news_id"]:
            raise ValueError("decision does not recommend retirement")
        source = await self.lifecycle.ledger.get_news(tenant_id=tenant_id,
            news_id=pair["result"]["redundant_news_id"])
        target = await self.lifecycle.ledger.get_news(tenant_id=tenant_id,
            news_id=pair["result"]["replacement_news_id"])
        if not source or not target:
            raise ValueError("source or replacement no longer exists")
        if source["status"] == "deduplicated":
            old = await self.lifecycle.ledger.get_job(tenant_id=tenant_id, job_id=source["current_job_id"])
            if old["payload"].get("semantic_decision_id") == decision_id:
                return old
        return await self.lifecycle.retire_duplicate(tenant_id=tenant_id, news_id=source["news_id"],
            source_revision=source["source_revision"], source_job_id=source["active_job_id"],
            replacement_news_id=target["news_id"], replacement_revision=target["source_revision"],
            replacement_job_id=target["active_job_id"], semantic_decision_id=decision_id,
            semantic_bundle_id=self.config.bundle_id)

    async def recover(self, row):
        if row["status"] != "deduplicated":
            return
        job = await self.lifecycle.ledger.get_job(tenant_id=row["tenant_id"], job_id=row["current_job_id"])
        if not job["payload"].get("semantic_decision_id"):
            return
        target = await self.lifecycle.ledger.get_news(tenant_id=row["tenant_id"],
            news_id=job["payload"]["replacement_news_id"])
        if (not target or target["status"] != "active"
                or target["source_revision"] != job["payload"]["replacement_revision"]
                or job["payload"]["semantic_bundle_id"] != self.config.bundle_id):
            await self.lifecycle.restore_duplicate(tenant_id=row["tenant_id"], news_id=row["news_id"],
                retirement_job_id=job["job_id"])

    async def scan_page(self, after_key=None):
        rows = await self.lifecycle.ledger.list_news(limit=20, after_key=after_key, include_retracted=True)
        for row in rows:
            try:
                if row["status"] == "deduplicated":
                    await self.recover(row)
                elif row["status"] == "active":
                    await self.process(row)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("semantic scan deferred error=%s", type(exc).__name__)
        return (rows[-1]["tenant_id"], rows[-1]["news_id"]) if rows else None

    async def status(self, tenant_id, news_id):
        row = await self.lifecycle.ledger.get_news(tenant_id=tenant_id, news_id=news_id)
        if not row:
            return None
        analysis = await self.store.get(row)
        decisions = await self.store.live_pairs(tenant_id, [news_id], include_retired=True)
        role = "unclassified"
        if analysis and analysis["features"]:
            same = [p["result"] for p in decisions if p["result"]["relation"] == "same_event"]
            role = "redundant_candidate" if any(p["redundant_news_id"] == news_id for p in same) else (
                "supplement" if any(p["preferred_news_id"] != news_id for p in same) else "primary")
        return {"news_id": news_id, "source_revision": row["source_revision"], "index_status": row["status"],
                "semantic_status": analysis["status"] if analysis else "pending", "role": role,
                "event_id": analysis["event_id"] if analysis else None,
                "features": analysis["features"] if analysis else {}, "decisions": decisions,
                "last_error": analysis["last_error"] if analysis else None,
                "bundle_id": self.config.bundle_id, "auto_retire": self.config.auto_retire}

    async def enrich(self, *, tenant_id, hits, scores, limit, exclude_news_ids, seed_news_ids=()):
        """Bounded one-hop expansion; never silently collapse complementary evidence."""
        seeds = list(dict.fromkeys([h.news_id for h in hits] + list(seed_news_ids)))
        pairs = await self.store.live_pairs(tenant_id, seeds)
        by_id, removed, protected = {h.news_id: h for h in hits}, set(), set()
        expansion, trace = [], []
        for pair in pairs:
            result = pair["result"]
            if result["relation"] in ("uncertain", "unrelated") or result["unresolved"]:
                continue
            for target_id in (pair["left_news_id"], pair["right_news_id"]):
                if (target_id in by_id or target_id in exclude_news_ids
                        or (target_id == result["redundant_news_id"] and result["replacement_news_id"] in by_id)
                        or any(h.news_id == target_id for h in expansion)
                        or len(expansion) >= min(self.config.query_expansion_limit, limit // 3)):
                    continue
                projection = await self.store.projection(tenant_id, target_id)
                if not projection or not projection["records"]:
                    continue
                tier = VectorTier(projection["target_tier"])
                if (projection["config"]["embedding_version"] != self.lifecycle.config.embedding_version
                        or self.lifecycle.config.query_weights[tier] <= 0):
                    continue
                quotes = await self.store.evidence_quotes(pair, target_id)
                record = next((r for r in projection["records"] if any(
                    q in r["excerpt"] or (len(r["excerpt"]) >= 8 and r["excerpt"] in q)
                    or q[:32] in r["excerpt"] for q in quotes)), projection["records"][0])
                chunk_ids = await self.lifecycle.index.contains(tier=tier, chunk_ids=[record["chunk_id"]])
                if record["chunk_id"] not in chunk_ids:
                    continue
                expansion.append(VectorSearchHit(chunk_id=record["chunk_id"], news_id=target_id,
                    title=record["title"], excerpt=record["excerpt"], source_url=record["source_url"],
                    publish_time=datetime.fromisoformat(record["publish_time"]), raw_score=0.0, tier=tier,
                    content_version=record["content_version"], embedding_version=record["embedding_version"]))
                scores[target_id] = min(scores.values(), default=0.0) * 0.5
                trace.append({"decision_id": pair["decision_id"], "relation": result["relation"],
                              "direction": result["direction"], "news_id": target_id})
        # Relationships have two versioned endpoints; checking only the added hit
        # would miss a source correction during expansion.
        current_pairs = await self.store.live_pairs(tenant_id, seeds)
        live = {p["decision_id"] for p in current_pairs}
        trace = [edge for edge in trace if edge["decision_id"] in live]
        expanded_ids = {edge["news_id"] for edge in trace}
        expansion = [h for h in expansion if h.news_id in expanded_ids]
        present = set(by_id) | {h.news_id for h in expansion}
        for pair in current_pairs:
            result = pair["result"]
            source, target = result["redundant_news_id"], result["replacement_news_id"]
            if (source in present and target in present and source not in protected
                    and target not in removed and not result["unresolved"]):
                removed.add(source)
                protected.add(target)
                scores[target] = max(scores[target], scores[source])
        expansion = [h for h in expansion if h.news_id not in removed]
        kept = sorted((h for h in hits if h.news_id not in removed), key=lambda h: (-scores[h.news_id], h.news_id))
        selected = kept[:max(1, limit - len(expansion))] + expansion
        selected = await self.lifecycle.ledger.filter_hits(tenant_id=tenant_id, hits=selected)
        returned = {h.news_id for h in selected}
        trace = [edge for edge in trace if edge["news_id"] in returned]
        return selected[:limit], {"collapsed": sorted(removed), "expanded": trace}


async def semantic_loop(service):
    """Independent worker task: slow model calls cannot starve vector indexing."""
    cursor = None
    while True:
        try:
            cursor = await service.scan_page(cursor)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("semantic scanner will retry error=%s", type(exc).__name__)
        await asyncio.sleep(service.config.poll_seconds)
