"""Offline ranking ablation and honest pooled scoring over human judgments.

The production LifecycleKnowledgeSearch class supplies both fusion branches.
Ports below are immutable exact-search fixtures, not production DB/ANN adapters.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import random
import re
import sqlite3
import statistics
from types import SimpleNamespace

from fastapi import HTTPException
from app.modules.knowledge.tier_lifecycle_query import LifecycleKnowledgeSearch
from app.modules.retrieval_lab.metrics import LABEL_GRADES
from app.modules.retrieval_lab.store import RetrievalLabStore
from app.retrieval.tiered_vector import VectorSearchHit, VectorTier

EXPERIMENT_VERSION = "annual-frozen-fusion-ablation-v1"
REVIEW_VERSION = "annual-fusion-blind-supplement-v1"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def sha_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_legacy_ranking(path):
    """Read the two flow-style ranking fields in the checked-in local example.

    This deliberately does not parse arbitrary YAML or initialize app settings.
    Reject other layouts rather than silently inventing ranking defaults.
    """
    text = Path(path).read_text()
    k_rows = re.findall(r"^query_rrf_k:\s*([0-9]+)\s*(?:#.*)?$", text, re.MULTILINE)
    w_rows = re.findall(r"^query_weights:\s*\{([^}]+)\}\s*(?:#.*)?$", text, re.MULTILINE)
    if len(k_rows) != 1 or len(w_rows) != 1 or int(k_rows[0]) < 1:
        raise ValueError("legacy ranking requires one positive k and one flow-style weights map")
    weights = {}
    for entry in w_rows[0].split(","):
        key, value = (s.strip() for s in entry.split(":", 1))
        if key in weights or key not in {"hot", "warm", "cold"}:
            raise ValueError("legacy tier keys must occur exactly once")
        weight = float(value)
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError("legacy tier weights must be positive and finite")
        weights[key] = weight
    if set(weights) != {"hot", "warm", "cold"}:
        raise ValueError("legacy ranking needs all three tier weights")
    return {"rrf_k": int(k_rows[0]), "weights": weights}


def load_review(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    for name, info in manifest["files"].items():
        if Path(name).name != name or sha_file(directory / name) != info["sha256"]:
            raise ValueError("frozen review integrity failed: " + name)
    export = json.loads((directory / "human-review.export.json").read_text())
    keys = set()
    for run in export["runs"]:
        if run["query_id"] in keys:
            raise ValueError("duplicate frozen query")
        keys.add(run["query_id"])
        for c in run["candidates"]:
            if c["judgment"]["status"] != "judged":
                raise ValueError("source review is incomplete")
            if hashlib.sha256(c["body"].encode()).hexdigest() != c["body_sha256"]:
                raise ValueError("candidate body hash mismatch")
    return export


class FrozenIndex:
    """Same news-level candidate pool on both sides, isolating fusion only."""
    similarity_metric = "COSINE"

    def __init__(self, hits):
        self.hits = hits

    async def search_with_trace(self, *, tier, limit, exclude_news_ids, trace, **kwargs):
        chosen = [h for h in self.hits[tier] if h.news_id not in exclude_news_ids][:limit]
        trace.update(backend="frozen_exact_news_pool", returned_news=len(chosen),
                     candidate_unit="news", production_gate=False)
        return chosen, trace

    search_news_with_trace = search_with_trace


class FrozenLedger:
    def __init__(self, hits):
        self.allowed = {(h.tier, h.chunk_id, h.news_id, h.embedding_version)
                        for rows in hits.values() for h in rows}

    async def filter_hits(self, *, hits, **kwargs):
        return [h for h in hits if (h.tier, h.chunk_id, h.news_id, h.embedding_version) in self.allowed]


class FrozenEmbedder:
    def __init__(self, vector, version):
        self.vector, self.version = tuple(float(v) for v in vector), version

    async def embed_queries(self, queries, **kwargs):
        if len(queries) != 1:
            raise ValueError("one frozen query at a time")
        return (self.vector,)


def exact_news_candidates(engine, vector):
    similarities = engine.vectors @ vector
    hits = {}
    for tier in VectorTier:
        positions = [i for i, chunk in enumerate(engine.chunks) if chunk["tier"] == tier.value]
        positions.sort(key=lambda i: (-float(similarities[i]), engine.chunks[i]["chunk_id"]))
        rows, seen = [], set()
        for i in positions:
            chunk = engine.chunks[i]
            if chunk["news_id"] in seen:
                continue
            seen.add(chunk["news_id"])
            article = engine.by_id[chunk["news_id"]]
            published = (datetime.fromisoformat(article["publish_time"].replace("Z", "+00:00"))
                         if article.get("publish_time") else None)
            rows.append(VectorSearchHit(chunk_id=chunk["chunk_id"], news_id=chunk["news_id"],
                title=article["title"], excerpt=chunk["excerpt"], source_url=article.get("url"),
                publish_time=published, raw_score=float(similarities[i]), tier=tier,
                content_version=1, embedding_version=engine.model_version))
        hits[tier] = rows
    return hits


async def rank_pair(*, hits, vector, embedding_version, query, category, policy, candidate_limit, rrf_k, weights):
    if candidate_limit < 10 or type(rrf_k) is not int or rrf_k < 1:
        raise ValueError("invalid ranking parameters")
    if set(weights) != {t.value for t in VectorTier} or any(
        not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0
        for v in weights.values()
    ):
        raise ValueError("positive finite weights required for all frozen tiers")
    embedder = FrozenEmbedder(vector, embedding_version)
    index, ledger = FrozenIndex(hits), FrozenLedger(hits)
    common = dict(query_candidate_limit=candidate_limit, query_max_candidates=200,
        query_rrf_k=rrf_k, query_weights={t: weights[t.value] for t in VectorTier},
        query_max_concurrency=1, query_request_timeout_seconds=30.0,
        expected_embedding_dimensions=len(vector), embedding_version=embedding_version,
        policy_version=EXPERIMENT_VERSION)
    result = {}
    for name, retrieval in (("legacy_rrf", None), ("global_similarity", policy)):
        cfg = SimpleNamespace(**common, query_retrieval=retrieval)
        client = LifecycleKnowledgeSearch(index=index, ledger=ledger, embedder=embedder, config=cfg)
        result[name] = await client.search(query_text=query, tenant_id="public-news-local", limit=10,
            use_semantics=False, retrieval_intent="general",
            **({"retrieval_topic": category} if retrieval is not None else {})) if retrieval else await client.search(
                query_text=query, tenant_id="public-news-local", limit=10, use_semantics=False)
    return result


def predictions(result):
    return [{"news_id": h.news_id, "rank": rank, "tier": h.tier.value,
             "matched_chunk_id": h.chunk_id, "cosine_similarity": h.raw_score,
             "fusion_score": result["scores"][h.news_id]}
            for rank, h in enumerate(result["hits"], 1)]


def known_labels(export):
    return {(r["query_id"], c["news_id"]): {
        "body_sha256": c["body_sha256"], "judgment": deepcopy(c["judgment"]),
        "origin": "frozen_single_human_review", "source_run_id": r["run_id"]}
        for r in export["runs"] for c in r["candidates"]}


def supplemental_runs(comparison, articles, labels):
    result, pending = [], []
    for row in comparison["queries"]:
        union = set()
        for strategy in ("legacy_rrf", "global_similarity"):
            union.update(p["news_id"] for p in row["predictions"][strategy])
        candidates = []
        for nid in sorted(union):
            article = articles[nid]
            known = labels.get((row["query_id"], nid))
            if known is not None:
                if known["body_sha256"] != article["body_sha256"]:
                    raise ValueError("cannot reuse a label after evidence changed")
                continue
            # Only evidence is shown: strategy, score and original rank stay private.
            candidates.append({key: deepcopy(article[key]) for key in (
                "news_id", "title", "body", "body_sha256", "url", "publication_date",
                "publish_time", "categories", "tier", "source_evidence", "duplicate_sources"
            ) if key in article})
        random.Random(digest([comparison["config"], row["query_id"], "blind-order"])).shuffle(candidates)
        for rank, article in enumerate(candidates, 1):
            article["rank"] = rank
            pending.append({"query_id": row["query_id"], "news_id": article["news_id"],
                            "body_sha256": article["body_sha256"], "status": "pending_human"})
        if candidates:
            result.append({"run_id": "supplement-" + digest([comparison["config"], row["query_id"]])[:24],
                "query_id": row["query_id"], "query": row["query"], "category": row["category"],
                "as_of_date": comparison["as_of_date"], "requested_limit": len(candidates),
                "versions": {**comparison["versions"], "strategy": REVIEW_VERSION},
                "candidates": candidates, "trace": {"status": "complete", "backend": "blind_human_review",
                    "retrieval_plan": {"intent": "blind_supplemental_review"},
                    "scope": "randomized_annotation_order_not_retrieval_order"}})
    return result, pending


class SupplementalStore(RetrievalLabStore):
    def _get_run(self, connection, run_id):
        run = super()._get_run(connection, run_id)
        message = "补标顺序已打乱；完成后统一计算对照指标。"
        run["metrics"] = {"k": 10, "precision_at_k": None, "pool_ndcg_at_k": None,
                          "precision_reason": message, "pool_ndcg_reason": message}
        return run


def read_supplemental_labels(path, comparison):
    path = Path(path)
    if not path.is_file():
        return {}
    expected = {q["query_id"]: q for q in comparison["queries"]}
    out = {}
    with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as connection:
        connection.execute("BEGIN")
        for payload, checksum in connection.execute("SELECT payload,sha256 FROM lab_runs"):
            if hashlib.sha256(payload.encode()).hexdigest() != checksum:
                raise ValueError("supplemental run integrity failed")
            run = json.loads(payload)
            if run["query_id"] not in expected or run["query"] != expected[run["query_id"]]["query"]:
                raise ValueError("supplemental query drift")
            if run["versions"] != {**comparison["versions"], "strategy": REVIEW_VERSION}:
                raise ValueError("supplemental version drift")
            by_id = {c["news_id"]: c for c in run["candidates"]}
            for nid, label, reviewer, notes, revision, updated_at in connection.execute(
                "SELECT news_id,label,reviewer,notes,revision,updated_at FROM lab_judgments WHERE run_id=?", (run["run_id"],)
            ):
                c = by_id[nid]
                if hashlib.sha256(c["body"].encode()).hexdigest() != c["body_sha256"]:
                    raise ValueError("supplemental evidence drift")
                out[run["query_id"], nid] = {"body_sha256": c["body_sha256"], "origin": "supplemental_human",
                    "judgment": {"label": label, "grade": LABEL_GRADES[label], "reviewer": reviewer,
                        "notes": notes, "revision": revision, "updated_at": updated_at,
                        "status": "unable" if label == "unsure" else "judged"}}
    return out


def calculate_comparison(comparison, labels):
    missing, per_query = [], []
    for row in comparison["queries"]:
        pool = sorted({p["news_id"] for ps in row["predictions"].values() for p in ps})
        unknown = [nid for nid in pool if (row["query_id"], nid) not in labels
                   or labels[row["query_id"], nid]["judgment"]["status"] != "judged"]
        missing.extend({"query_id": row["query_id"], "news_id": nid} for nid in unknown)
        entry = {"query_id": row["query_id"], "category": row["category"], "query": row["query"],
                 "pool_size": len(pool), "missing_labels": len(unknown), "metrics": None}
        if not unknown:
            grades = {nid: LABEL_GRADES[labels[row["query_id"], nid]["judgment"]["label"]] for nid in pool}
            entry["metrics"] = {}
            for strategy, preds in row["predictions"].items():
                entry["metrics"][strategy] = {}
                for k in (5, 10):
                    ordered = [grades[p["news_id"]] for p in preds[:k]]
                    if len(ordered) != k:
                        raise ValueError("prediction must contain K actual results")
                    dcg = lambda gs: sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(gs))
                    ideal = dcg(sorted(grades.values(), reverse=True)[:k])
                    entry["metrics"][strategy][str(k)] = {
                        "precision": sum(g == 2 for g in ordered) / k,
                        "pooled_ndcg": dcg(ordered) / ideal if ideal > 0 else None}
        per_query.append(entry)
    summary = {"status": "needs_human_labels" if missing else "complete_single_human_pooled_ablation",
        "scope": "frozen_question_search_equal_news_budget_fusion_ablation",
        "formal_independent_test": False, "queries": len(per_query),
        "required_pool_pairs": sum(q["pool_size"] for q in per_query),
        "missing_pairs": len(missing), "missing": missing, "metrics": None,
        "production_p95": None, "recall": None, "per_query": per_query}
    # Fixed denominator: one missing query blocks the aggregate, rather than dropping hard cases.
    if not missing:
        metrics = {}
        for strategy in ("legacy_rrf", "global_similarity"):
            metrics[strategy] = {}
            for k in (5, 10):
                ms = [r["metrics"][strategy][str(k)] for r in per_query]
                eligible = [m["pooled_ndcg"] for m in ms if m["pooled_ndcg"] is not None]
                metrics[strategy][str(k)] = {"precision": statistics.mean(m["precision"] for m in ms),
                    "pooled_ndcg": statistics.mean(eligible) if eligible else None,
                    "precision_query_count": len(ms), "ndcg_query_count": len(eligible)}
        summary["metrics"] = metrics
    return summary


class ComparisonReviewService:
    def __init__(self, directory):
        directory = Path(directory)
        self.comparison = json.loads((directory / "comparison.json").read_text())
        self.store = SupplementalStore(directory / "supplemental.sqlite3")
        self.manifest = self.store.get_manifest()
        if self.manifest["comparison_sha256"] != sha_file(directory / "comparison.json"):
            raise ValueError("comparison identity drift")

    def search(self, *args, **kwargs):
        raise HTTPException(409, "此页面只用于冻结对照实验的补标，不添加新查询。")

    def status(self):
        m = self.manifest
        runs = self.store.list_runs()
        return {"title": "旧／新检索策略对照：新增候选盲标",
            "corpus": {"document_count": m["source_count"], "canonical_count": m["canonical_count"],
                "tier_counts": m["tier_counts"], "category_counts": m["category_counts"]},
            "model": {"name": "冻结 BGE-large-zh-v1.5；补标无需模型调用", "version": self.comparison["versions"]["model"]},
            "query_count": len(runs), "judgment_count": sum(r["progress"]["reviewed"] for r in runs),
            "ready": True, "persistence": "server_sqlite",
            "warnings": ["这里只显示尚未标注的新增候选；原 300 项标签已冻结并复用。",
                         "候选顺序已打乱，不展示策略归属和原始排名；请按原相关性标准判断。",
                         "本页面的候选顺序不用于质量评分；完成后离线统一计算共同候选池指标。",
                         "这是单人问句检索实验，未获得生产性能或独立测试结论。"]}
