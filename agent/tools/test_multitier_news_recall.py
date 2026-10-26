#!/usr/bin/env python3
"""Isolated, real-BGE three-tier recall exploration; never changes active indexes.

Three freshly persisted fp32 HNSW indexes exercise the native tiered search client.
Date thresholds are an explicit demonstration policy, not the production policy.
No human relevance labels are available: semantic quality always remains blocked.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))

from app.clients.knowledge_base import RelatedNewsSearchQuery
from app.retrieval.bge_snapshot import (
    BGE_DIMENSIONS, BGE_QUERY_INSTRUCTION, LocalBgeQueryEmbedder,
    _CORPUS_PATH, _MODEL_PATH, _checked_vector, _read_chunks,
    _validate_model_config, _verify_package,
)
from app.retrieval.tiered_vector import (
    TieredVectorKnowledgeSearchClient, TierSearchPolicy, VectorSearchHit, VectorTier,
)

TENANT = "isolated-multitier-recall-exploration"
TIERS = tuple(VectorTier)
BUDGET_SECONDS = 300


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1_048_576), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as target:
        json.dump(value, target, ensure_ascii=False, indent=2, allow_nan=False)
        target.write("\n")


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def as_of(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        if len(value) != 10:
            raise argparse.ArgumentTypeError("--as-of needs a date or timezone-aware timestamp")
        result = result.replace(tzinfo=timezone(timedelta(hours=8)))
    return result


def git_output(*args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True)
    return result.stdout


def safe_error(error: BaseException) -> dict:
    # Exception messages can contain model inputs or untrusted corpus text.
    return {"type": type(error).__name__, "message": "Execution stopped; inspect stage and contract failures, not source text logs."}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--query-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--as-of", type=as_of, default=as_of("2026-10-15"))
    parser.add_argument("--hot-days", type=int, default=1)
    parser.add_argument("--warm-days", type=int, default=4)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--threads", type=int, choices=(1, 2), default=2)
    parser.add_argument("--seed", type=int, default=20261020)
    args = parser.parse_args(argv)
    if not 0 <= args.hot_days < args.warm_days:
        parser.error("require 0 <= hot-days < warm-days")
    if not 1 <= args.limit <= 50:
        parser.error("limit must be 1..50")
    if args.seed < 0 or args.seed >= 2**31:
        parser.error("seed must be in 0..2**31-1")
    args.package = args.package.resolve()
    args.query_file = args.query_file.resolve()
    args.output_dir = args.output_dir.resolve()
    if args.output_dir.exists():
        parser.error("output directory already exists; old runs must not be overwritten")
    if args.output_dir.is_relative_to(args.package):
        parser.error("output directory must be outside the read-only source package")
    return args


def read_queries(path: Path) -> list[dict]:
    if path.stat().st_size > 262144:
        raise ValueError("query file exceeds 256 KiB")
    queries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if not isinstance(row, dict) or set(row) - {"id", "query", "source_news_id"}:
            raise ValueError("queries accept only id/query/optional source_news_id; no labels")
        if any(not isinstance(row.get(key), str) or not row[key].strip() for key in ("id", "query")):
            raise ValueError("query identity/text must be nonblank")
        if len(row["query"]) > 2000 or len(row["id"]) > 200:
            raise ValueError("query exceeds text/identity budget")
        if "source_news_id" in row and (not isinstance(row["source_news_id"], str) or not row["source_news_id"].strip()):
            raise ValueError("source_news_id must be nonblank when supplied")
        queries.append(row)
    if not 1 <= len(queries) <= 20 or len({q["id"] for q in queries}) != len(queries):
        raise ValueError("require 1..20 uniquely identified queries")
    return queries


def select_rows(chunks, args):
    articles = {}
    rows = {tier: [] for tier in TIERS}
    excluded = {"unknown_date": [], "future_date": []}
    for label, chunk in enumerate(chunks):
        # Historical undated chunks do not repeat their title header. Their
        # structured time/URL must still agree; only selected rows need a title.
        identity = (chunk.publish_time, chunk.source_url)
        if chunk.news_id in articles and articles[chunk.news_id] != identity:
            raise ValueError("article metadata differs between chunks")
        articles[chunk.news_id] = identity
        if chunk.publish_time is None:
            excluded["unknown_date"].append(label)
            continue
        if chunk.publish_time > args.as_of:
            excluded["future_date"].append(label)
            continue
        if not chunk.title or not chunk.source_url:
            raise ValueError("dated articles need a title and structured source URL")
        age = (args.as_of.date() - chunk.publish_time.astimezone(args.as_of.tzinfo).date()).days
        tier = VectorTier.HOT if age <= args.hot_days else VectorTier.WARM if age <= args.warm_days else VectorTier.COLD
        rows[tier].append(label)
    def counts(labels):
        return {"articles": len({chunks[i].news_id for i in labels}), "chunks": len(labels)}
    distribution = {tier.value: counts(labels) for tier, labels in rows.items()}
    distribution.update({key: counts(labels) for key, labels in excluded.items()})
    distribution["source_total"] = {"articles": len(articles), "chunks": len(chunks)}
    if any(not labels for labels in rows.values()):
        raise ValueError("all three date tiers must contain real rows")
    return rows, distribution


class CachedRealQueryEmbedding:
    """Strict per-request bridge: cached vector came from one real BGE encoding."""

    def __init__(self, *, text, vector, version):
        self.text, self.vector, self.version = text, vector, version
        self.calls = 0

    async def embed_queries(self, texts, *, tenant_id):
        if tenant_id != TENANT or list(texts) != [self.text] or self.calls:
            raise ValueError("query-vector bridge identity/call budget mismatch")
        self.calls += 1
        return [self.vector]


class IsolatedThreeTierIndex:
    """Read-only Port over newly saved and reloaded indexes; no production routing."""

    def __init__(self, *, indexes, rows, chunks, version, manifest_digest, np, threads):
        self.indexes, self.rows, self.chunks = indexes, rows, chunks
        self.version, self.manifest_digest = version, manifest_digest
        self.np, self.threads = np, threads
        self.last = {}

    async def search(self, *, tier, tenant_id, vector, embedding_version, exclude_news_ids, limit):
        started = time.perf_counter()
        if tenant_id != TENANT or embedding_version != self.version or tier not in self.indexes:
            raise ValueError("tier/query identity mismatch")
        vector = _checked_vector(vector)
        index, row_ids = self.indexes[tier], self.rows[tier]
        pool = min(max(200, limit), len(row_ids))
        pools = []
        while True:
            pools.append(pool)
            index.set_ef(max(256, pool))
            labels, distances = index.knn_query(self.np.asarray([vector], dtype=self.np.float32), k=pool, num_threads=self.threads)
            found = {}
            for label, distance in zip(labels[0], distances[0], strict=True):
                label, score = int(label), 1.0 - float(distance)
                if not 0 <= label < len(row_ids) or not math.isfinite(score):
                    raise ValueError("invalid HNSW response")
                source_label = row_ids[label]
                chunk = self.chunks[source_label]
                if chunk.news_id in exclude_news_ids:
                    continue
                hit = VectorSearchHit(
                    chunk_id=f"isolated-bge:{self.manifest_digest}:{source_label}", news_id=chunk.news_id,
                    title=chunk.title, excerpt=chunk.text, source_url=chunk.source_url,
                    publish_time=chunk.publish_time, raw_score=score, tier=tier,
                    content_version=1, embedding_version=self.version,
                )
                old = found.get(chunk.news_id)
                if old is None or hit.raw_score > old.raw_score:
                    found[chunk.news_id] = hit
            ranked = sorted(found.values(), key=lambda hit: (-hit.raw_score, hit.chunk_id))[:limit]
            if len(ranked) >= limit or pool == len(row_ids):
                break
            pool = min(pool * 2, len(row_ids))
        self.last[tier] = {"hits": ranked, "elapsed_ms": (time.perf_counter() - started) * 1000,
                           "candidate_pools": pools, "requested_news": limit,
                           "returned_news": len(ranked), "ef": max(256, pool)}
        return ranked

    async def upsert(self, **kwargs):
        raise PermissionError("isolated index Port is read-only")

    async def delete(self, **kwargs):
        raise PermissionError("isolated index Port is read-only")

    async def contains(self, **kwargs):
        raise PermissionError("lifecycle operations are outside this exploration")


def hit_json(hit, rank):
    return {"rank": rank, "news_id": hit.news_id, "title": hit.title,
            "publish_time": hit.publish_time.isoformat(), "url": hit.source_url,
            "excerpt": hit.excerpt[:320], "tier": hit.tier.value, "chunk_id": hit.chunk_id,
            "raw_ip_score": hit.raw_score, "rrf_contribution": 1 / (60 + rank),
            "embedding_version": hit.embedding_version, "content_version": hit.content_version}


def report_text(results):
    lines = ["# 真实 BGE 三层新闻召回探索", "", f"- 运行：`{results['run_id']}`",
             f"- 索引契约：**{results['status']['index_contract']}**；召回契约：**{results['status']['recall_contract']}**；语义质量：**blocked**（未有人审相关性标签）。",
             "- 范围：本地公开新闻、真实 BGE、三个实际 fp32 HNSW；日期是演练分层。没有 HTTP、PG、Temporal、业务规则重排或企业接口。",
             "- 查询由助手依据来源材料设计，是探索草案；每题只执行一次。RRF 分数不是置信度。没有 ANN agreement、语义 precision/recall、p95 或 SLO 结论。",
             "- 单个查询只实际编码一次，三个层与原生融合共用该真实向量；缓存桥接不是模拟 Embedding。",
             "", "## 数据分布", "", "| 层/排除原因 | 新闻数 | 切片数 |", "|---|---:|---:|"]
    for name, count in results.get("distribution", {}).items():
        lines.append(f"| {name} | {count['articles']} | {count['chunks']} |")
    lines += ["", f"计划 {results['planned_queries']} 题；完成 {len(results['queries'])} 题；未完成 {results['planned_queries'] - len(results['queries'])} 题。",
              f"总耗时 {results['elapsed_seconds']:.3f} 秒，限制 {BUDGET_SECONDS} 秒。", ""]
    for query in results["queries"]:
        lines += [f"## {query['id']}：{query['query']}", "", f"类型：{query['case_kind']}；契约：{query['status']}；真实编码 {query['embedding_ms']:.2f} ms；原生检索融合 {query['native_recall_ms']:.2f} ms。", ""]
        for name, info in query["tiers"].items():
            lines += [f"### {name}（{info['elapsed_ms']:.2f} ms，返回 {info['returned_news']} 候选新闻，池 {info['candidate_pools']}）", ""]
            for item in info["top_k"]:
                lines += [f"{item['rank']}. [{item['title']}]({item['url']}) — `{item['news_id']}`；{item['publish_time']}；IP={item['raw_ip_score']:.5f}；RRF贡献={item['rrf_contribution']:.6f}", f"   > {item['excerpt'].replace(chr(10), ' ')}", ""]
        lines += ["### 全层等权 RRF 融合", ""]
        for item in query["fused"]:
            lines.append(f"{item['rank']}. [{item['title']}]({item['url']}) — {item['tier']}；`{item['news_id']}`；RRF={item['rrf_score']:.6f}（原生归一化={item['normalized_rrf_score']:.6f}）")
        lines += ["", f"等权 TopK 层分布：`{json.dumps(query['equal_weight_tier_counts'], ensure_ascii=False)}`", "", f"### 原生默认策略（冷层权重0.7，{query['default_policy_recall_ms']:.2f} ms）", ""]
        for item in query["default_policy_items"]:
            lines.append(f"{item['rank']}. [{item['title']}]({item['url']}) — {item['tier']}；`{item['news_id']}`；RRF={item['rrf_score']:.6f}（原生归一化={item['normalized_rrf_score']:.6f}）")
        lines += ["", f"默认策略 TopK 层分布：`{json.dumps(query['default_policy_tier_counts'], ensure_ascii=False)}`"]
        lines += ["", f"检查：`{json.dumps(query['checks'], ensure_ascii=False)}`", ""]
    lines += ["## 调用链与复验", "", "`test_multitier_news_recall.py` → 原生 `_verify_package` / `_read_chunks` → 结构化日期过滤与演练分层 → 三个 hnswlib IP 索引构建/保存/新实例重载 → `LocalBgeQueryEmbedder`（真实本地 SentenceTransformer）→ 严格的真实向量缓存桥接 → `TieredVectorKnowledgeSearchClient` → 隔离只读 Index Port → 原生等权 RRF → `results.json` / 本报告。", "", "配置及源码/数据/查询/依赖哈希见不可覆盖的 `run-spec.json`；每层 `index.bin`、`row-mapping.jsonl` 和逐题 query-vector.npy 是复验资产。trace 是本地阶段计时，不是 OTel。", "", "TODO：人工审核数据集并建立独立相关性标签；生产30/365天分层、真实量化和迁移生命周期；企业接入；代表性流量及规模/长稳验证。此运行不修改生产索引/配置，不触发训练或发布。", ""]
    if results.get("error"):
        lines += [f"执行错误类型：`{results['error']['type']}`。失败/未执行题保留在计划分母。", ""]
    return "\n".join(lines)


async def execute(args, queries, chunks, rows, manifest_digest, results):
    # Explicitly prevent online model lookup and constrain CPU use before imports.
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = str(args.threads)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    import numpy as np
    import hnswlib
    import torch
    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    torch.manual_seed(args.seed)
    from sentence_transformers import SentenceTransformer

    source_vectors = np.load(args.package / _CORPUS_PATH / "embeddings.npy", mmap_mode="r", allow_pickle=False)
    if source_vectors.shape != (len(chunks), BGE_DIMENSIONS) or source_vectors.dtype != np.float32:
        raise ValueError("vector identity/shape/dtype mismatch")
    indexes = {}
    for tier in TIERS:
        results["stage"] = f"build_{tier.value}"
        stage_start = time.perf_counter()
        ids = rows[tier]
        vectors = np.asarray(source_vectors[ids])
        if not np.isfinite(vectors).all() or not np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=.001):
            raise ValueError("source vectors are not finite normalized fp32")
        folder = args.output_dir / tier.value
        folder.mkdir()
        index = hnswlib.Index(space="ip", dim=BGE_DIMENSIONS)
        index.init_index(max_elements=len(ids), ef_construction=200, M=32, random_seed=args.seed)
        index.set_num_threads(args.threads)
        index.add_items(vectors, np.arange(len(ids)), num_threads=args.threads)
        index.save_index(str(folder / "index.bin"))
        del index
        loaded = hnswlib.Index(space="ip", dim=BGE_DIMENSIONS)
        loaded.load_index(str(folder / "index.bin"), max_elements=len(ids))
        loaded.set_num_threads(args.threads)
        if loaded.get_current_count() != len(ids) or sorted(loaded.get_ids_list()) != list(range(len(ids))):
            raise ValueError("persisted index label/count mismatch")
        for start in range(0, len(ids), 512):
            if not np.allclose(loaded.get_items(list(range(start, min(start + 512, len(ids))))), vectors[start:start + 512], atol=1e-6):
                raise ValueError("reloaded index vector differs from source")
        indexes[tier] = loaded
        with (folder / "row-mapping.jsonl").open("x", encoding="utf-8") as target:
            for label, source_label in enumerate(ids):
                chunk = chunks[source_label]
                target.write(json.dumps({"hnsw_label": label, "source_row": source_label, "news_id": chunk.news_id, "chunk_index": chunk.chunk_index}, ensure_ascii=False) + "\n")
        results["indexes"][tier.value] = {"chunks": len(ids), "elapsed_ms": (time.perf_counter() - stage_start) * 1000,
                                                 "bytes": (folder / "index.bin").stat().st_size,
                                                 "sha256": sha(folder / "index.bin"), "mapping_sha256": sha(folder / "row-mapping.jsonl"),
                                                 "persisted_reload_vector_check": "passed"}
        del vectors
    results["status"]["index_contract"] = "passed"
    results["stage"] = "load_real_model"
    model_start = time.perf_counter()
    model = SentenceTransformer(str(args.package / _MODEL_PATH), device="cpu", local_files_only=True, trust_remote_code=False)
    model.max_seq_length = 512
    if model.get_sentence_embedding_dimension() != BGE_DIMENSIONS:
        raise ValueError("model vector dimensions differ from source")
    version = f"bge-large-zh-v1.5:{manifest_digest[:16]}"
    embedder = LocalBgeQueryEmbedder(model, tenant_id=TENANT, version=version, batch_size=1)
    results["model_load_ms"] = (time.perf_counter() - model_start) * 1000
    warm_start = time.perf_counter()
    await embedder.embed_queries(["新闻检索预热"], tenant_id=TENANT)
    results["warmup"] = {"text": "新闻检索预热", "calls": 1, "elapsed_ms": (time.perf_counter() - warm_start) * 1000, "excluded_from_query_results": True}
    index_port = IsolatedThreeTierIndex(indexes=indexes, rows=rows, chunks=chunks, version=version, manifest_digest=manifest_digest, np=np, threads=args.threads)
    policy = TierSearchPolicy(version="isolated-equal-weight-rrf60-v1", rrf_k=60, per_tier_limit=50, hot_weight=1, warm_weight=1, cold_weight=1)
    all_news = {chunk.news_id for chunk in chunks}
    case_kinds = {}
    cases_path = args.query_file.parent / "cases.jsonl"
    if cases_path.exists():
        for line in cases_path.read_text(encoding="utf-8").splitlines():
            case = json.loads(line)
            case_kinds[case.get("id", case.get("query_id"))] = case.get("kind", case.get("case_kind", "exploration"))
    try:
        for number, item in enumerate(queries):
            results["stage"] = f"query_{number + 1}"
            started = stamp()
            embed_start = time.perf_counter()
            vector = (await embedder.embed_queries([item["query"].strip()], tenant_id=TENANT))[0]
            embed_ms = (time.perf_counter() - embed_start) * 1000
            vector_path = args.output_dir / f"query-{number + 1:02d}-vector.npy"
            with vector_path.open("xb") as target:
                np.save(target, np.asarray(vector, dtype=np.float32), allow_pickle=False)
            cache = CachedRealQueryEmbedding(text=item["query"].strip(), vector=vector, version=version)
            client = TieredVectorKnowledgeSearchClient(embedder=cache, index=index_port, policy=policy, tiers=TIERS, max_query_concurrency=1)
            source_id = item.get("source_news_id", f"__isolated_query__:{item['id']}")
            if "source_news_id" not in item and source_id in all_news:
                raise ValueError("synthetic source sentinel collides with corpus")
            query = RelatedNewsSearchQuery(query_id=item["id"], source_news_id=source_id, title=item["query"], summary="", candidate_limit=args.limit)
            index_port.last = {}
            recall_start = time.perf_counter()
            native = (await client.batch_search_related_news((query,), tenant_id=TENANT))[item["id"]]
            recall_ms = (time.perf_counter() - recall_start) * 1000
            tier_json, representatives, scores = {}, {}, {}
            for tier in TIERS:
                trace = index_port.last[tier]
                hits = trace["hits"]
                for rank, hit in enumerate(hits, 1):
                    representatives[hit.news_id] = hit
                    scores[hit.news_id] = scores.get(hit.news_id, 0) + 1 / (60 + rank)
                tier_json[tier.value] = {key: value for key, value in trace.items() if key != "hits"}
                tier_json[tier.value]["candidate_news_ids"] = [hit.news_id for hit in hits]
                tier_json[tier.value]["top_k"] = [hit_json(hit, rank) for rank, hit in enumerate(hits[:args.limit], 1)]
            fused = []
            for rank, result in enumerate(native, 1):
                hit = representatives[result.news_id]
                projected = hit_json(hit, rank)
                projected.pop("rrf_contribution")
                fused.append({**projected, "rrf_score": scores[result.news_id], "normalized_rrf_score": result.score})
            # Pre-registered diagnostic: the native default cold weight is 0.7.
            # Same real vector, same three indexes and candidate budgets; no tuning.
            default_cache = CachedRealQueryEmbedding(text=item["query"].strip(), vector=vector, version=version)
            default_client = TieredVectorKnowledgeSearchClient(embedder=default_cache, index=index_port, policy=TierSearchPolicy(), tiers=TIERS, max_query_concurrency=1)
            index_port.last = {}
            default_start = time.perf_counter()
            default_native = (await default_client.batch_search_related_news((query,), tenant_id=TENANT))[item["id"]]
            default_ms = (time.perf_counter() - default_start) * 1000
            default_scores, default_representatives = {}, {}
            default_trace = {}
            for tier in TIERS:
                trace = index_port.last[tier]
                weight = .7 if tier == VectorTier.COLD else 1
                default_trace[tier.value] = {key: value for key, value in trace.items() if key != "hits"}
                default_trace[tier.value]["candidate_news_ids"] = [hit.news_id for hit in trace["hits"]]
                for rank, hit in enumerate(trace["hits"], 1):
                    default_representatives[hit.news_id] = hit
                    default_scores[hit.news_id] = default_scores.get(hit.news_id, 0) + weight / (60 + rank)
            default_items = [{**hit_json(default_representatives[result.news_id], rank),
                              "rrf_score": default_scores[result.news_id], "normalized_rrf_score": result.score}
                             for rank, result in enumerate(default_native, 1)]
            for result in default_items:
                result.pop("rrf_contribution")
            expected_ids = sorted(scores, key=lambda news_id: (-scores[news_id], news_id))[:args.limit]
            checks = {
                "three_tiers_executed": set(index_port.last) == set(TIERS),
                "all_tiers_have_candidates": all(index_port.last[tier]["hits"] for tier in TIERS),
                "source_excluded": all(source_id not in trace["candidate_news_ids"] for trace in tier_json.values()) and all(item["news_id"] != source_id for item in fused),
                "article_deduplicated": all(len(trace["candidate_news_ids"]) == len(set(trace["candidate_news_ids"])) for trace in tier_json.values()) and len({item["news_id"] for item in fused}) == len(fused),
                "native_rrf_matches_frozen_formula": [item.news_id for item in native] == expected_ids and all(math.isclose(item.score, scores[item.news_id] / (3 / 61), abs_tol=1e-12) for item in native),
                "metadata_time_model_valid": all(hit.publish_time is not None and hit.publish_time <= args.as_of and hit.source_url and hit.title and hit.embedding_version == version for hit in representatives.values()),
                "cache_served_exactly_once": cache.calls == 1,
                "default_cache_served_exactly_once": default_cache.calls == 1,
                "default_native_rrf_matches_frozen_formula": [result.news_id for result in default_native] == sorted(default_scores, key=lambda news_id: (-default_scores[news_id], news_id))[:args.limit] and all(math.isclose(result.score, default_scores[result.news_id] / (2.7 / 61), abs_tol=1e-12) for result in default_native),
                "default_source_excluded_and_unique": all(source_id not in trace["candidate_news_ids"] and len(trace["candidate_news_ids"]) == len(set(trace["candidate_news_ids"])) for trace in default_trace.values()) and len({result.news_id for result in default_native}) == len(default_native),
                "same_candidates_in_both_policies": all(tier_json[tier.value]["candidate_news_ids"] == default_trace[tier.value]["candidate_news_ids"] for tier in TIERS),
            }
            entry = {"id": item["id"], "query": item["query"], "source_news_id": item.get("source_news_id"), "case_kind": case_kinds.get(item["id"], "exploration"),
                     "started_at": started, "finished_at": stamp(), "attempts": 1,
                     "embedding_ms": embed_ms, "native_recall_ms": recall_ms,
                     "query_vector_file": vector_path.name, "query_vector_sha256": sha(vector_path),
                     "trace": ["request_validated", "real_bge_encoded", "query_vector_persisted", "native_tiered_search", "native_rrf_fused", "contract_checked"],
                     "tiers": tier_json, "fused": fused, "equal_weight_items": fused,
                     "equal_weight_tier_counts": dict(Counter(result["tier"] for result in fused)),
                     "default_policy_items": default_items, "default_policy_tier_counts": dict(Counter(result["tier"] for result in default_items)),
                     "default_policy_recall_ms": default_ms, "default_policy_tier_trace": default_trace,
                     "checks": checks, "status": "passed" if all(checks.values()) else "failed"}
            results["queries"].append(entry)
            write_json(args.output_dir / f"query-{number + 1:02d}-result.json", entry)
            if entry["status"] != "passed":
                raise ValueError("query recall contract failed")
        results["status"]["recall_contract"] = "passed"
        results["stage"] = "completed"
    finally:
        await embedder.close()
        mapped = getattr(source_vectors, "_mmap", None)
        if mapped is not None:
            mapped.close()


def _run(argv=None) -> int:
    args = parse_args(argv)
    started = time.perf_counter()
    results = {"run_id": args.output_dir.name, "started_at": stamp(), "stage": "validate_and_freeze", "queries": [], "indexes": {},
               "status": {"index_contract": "not_run", "recall_contract": "not_run", "semantic_quality": "blocked"}}
    try:
        queries = read_queries(args.query_file)
        results["planned_queries"] = len(queries)
        manifest_digest = _verify_package(args.package)
        _validate_model_config(args.package / _MODEL_PATH)
        chunks = _read_chunks(args.package / _CORPUS_PATH / "chunks.jsonl", tenant_id=TENANT)
        rows, distribution = select_rows(chunks, args)
        results["distribution"] = distribution
        relevant = [Path(__file__).resolve(), ROOT / "agent/app/clients/knowledge_base.py", ROOT / "agent/app/retrieval/bge_snapshot.py", ROOT / "agent/app/retrieval/tiered_vector.py"]
        source_files = {str(path.relative_to(ROOT)): sha(path) for path in relevant}
        tracked_diff = git_output("diff", "HEAD", "--", *(str(path.relative_to(ROOT)) for path in relevant))
        dependencies = {}
        for name in ("numpy", "hnswlib", "torch", "sentence-transformers", "transformers", "tokenizers"):
            dependencies[name] = importlib.metadata.version(name)
        artifacts = {"SHA256SUMS": {"path": str(args.package / "SHA256SUMS"), "sha256": manifest_digest},
                     "queries": {"path": str(args.query_file), "sha256": sha(args.query_file)}}
        for name in ("cases.jsonl", "README.md"):
            path = args.query_file.parent / name
            if path.is_file():
                artifacts[name] = {"path": str(path), "sha256": sha(path), "used_for_model_input": False}
        for name in ("chunks.jsonl", "embeddings.npy"):
            path = args.package / _CORPUS_PATH / name
            artifacts[name] = {"path": str(path), "sha256": sha(path)}
        spec = {"protocol": "newsagent-isolated-real-three-tier-exploration-v1", "frozen_at": stamp(),
                "authorization": "user requested testing multilayer news recall; isolated output only",
                "scope": {"start": "frozen public news chunks/vectors and local BGE model", "end": "native tiered client RRF result and local report",
                          "includes": ["real BGE query encoding", "three real fp32 HNSW builds", "persist/reload/vector verification", "article deduplication", "source exclusion", "native equal-weight RRF"],
                          "excludes": ["HTTP", "PG", "Temporal", "business reranking", "quantization", "lifecycle migration", "enterprise services", "ANN agreement", "semantic quality scoring", "production performance/SLO"],
                          "allowed_conclusions": ["isolated index and recall contracts", "observable per-query returned public news"],
                          "semantic_quality": {"status": "blocked", "reason": "assistant-authored source-informed draft cases; no independently reviewed qrels; no representative traffic"}},
                "inputs": artifacts, "package_verification": "passed: native _verify_package checks required corpus and every model file against manifest",
                "queries": {"count": len(queries), "ids": [query["id"] for query in queries], "source": "assistant-authored source-informed exploratory draft", "human_review": "not_reviewed", "split": "exploration_no_quality_claim"},
                "distribution": distribution, "as_of": args.as_of.isoformat(), "timezone": "Asia/Shanghai",
                "tier_policy": {"kind": "calendar-date-age demonstration", "hot_days_inclusive": args.hot_days, "warm_days_inclusive": args.warm_days, "unknown_date": "excluded", "future_date": "excluded", "production_30_365_days": "not_run"},
                "source_identity": {"git_head": git_output("rev-parse", "HEAD").decode().strip(), "relevant_files_sha256": source_files, "relevant_tracked_diff_sha256": hashlib.sha256(tracked_diff).hexdigest()},
                "runtime": {"python": sys.version, "executable": sys.executable, "dependencies": dependencies, "system": platform.system(), "machine": platform.machine(), "processor": platform.processor() or "unavailable", "logical_cpu_count": os.cpu_count(), "threads": args.threads, "intra_query_tier_dispatch": "async gather with synchronous local Port; max 2 HNSW worker threads", "device": "cpu"},
                "versions": {"embedding": f"bge-large-zh-v1.5:{manifest_digest[:16]}", "query_instruction": BGE_QUERY_INSTRUCTION, "max_seq_length": 512, "inference": "not_run", "prompt": "not_applicable", "SQL": "not_run", "schema_validator": "native RelatedNewsSearchQuery and checked BGE vectors", "index": "hnswlib ip fp32", "reranker": "native rank-only RRF"},
                "execution": {"command": shlex.join([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]), "seed": args.seed, "repetitions_per_policy": 1, "warmup": "one real short-sentence query, separately timed", "max_queries": 20, "total_budget_seconds": BUDGET_SECONDS, "max_query_encoding_calls": len(queries) + 1, "max_port_search_calls": len(queries) * 3 * 2, "knn_query_expansion_budget": "at most 1+ceil(log2(tier_chunks/initial_pool)) calls per Port.search; stops once 50 unique news found or tier exhausted", "parallel_queries": 1, "external_network_calls": 0, "stop_on_contract_failure": True},
                "search": {"M": 32, "ef_construction": 200, "initial_chunk_pool": 200, "ef": "max(256,pool)", "pool_expansion": "double until 50 unique articles or tier exhausted", "per_tier_news_limit": 50, "output_top_k": args.limit, "rrf_k": 60, "policies": {"equal_weight": {"version": "isolated-equal-weight-rrf60-v1", "tier_weights": {tier.value: 1 for tier in TIERS}}, "native_default": {"version": "tiered-rrf-v1", "tier_weights": {"hot": 1, "warm": 1, "cold": .7}}}, "comparison_scope": "two pre-registered policies, no tuning or semantic improvement claim; share one real query encoding"},
                "oracle": {"index": "persisted reload count/labels/vector allclose atol=1e-6", "recall": "source exclusion, unique news, three-tier execution, structured metadata/as_of/model identity, native RRF formula", "semantic": "unavailable", "standard": "engineering exploration assertions, no approved semantic acceptance threshold"},
                "side_effects": "new exclusive output directory only; source package/models/indexes and production configurations read-only",
                "baseline": {"current": "existing frozen v2 vectors", "candidate": "isolated equal-weight three fp32 date tiers", "previous": "not_used; no improvement claim"}}
        args.output_dir.mkdir(parents=True, exist_ok=False)
        write_json(args.output_dir / "run-spec.json", spec)
        write_json(args.output_dir / "commands.json", {"command": spec["execution"]["command"], "started_at": results["started_at"], "budget_seconds": BUDGET_SECONDS})
        asyncio.run(execute(args, queries, chunks, rows, manifest_digest, results))
    except BaseException as error:
        results["error"] = safe_error(error)
        if results["status"]["index_contract"] != "passed":
            results["status"]["index_contract"] = "failed"
        else:
            results["status"]["recall_contract"] = "failed"
    results["finished_at"] = stamp()
    results["elapsed_seconds"] = time.perf_counter() - started
    if args.output_dir.is_dir() and (args.output_dir / "run-spec.json").is_file():
        write_json(args.output_dir / "results.json", results)
        with (args.output_dir / "report.md").open("x", encoding="utf-8") as target:
            target.write(report_text(results))
    print(json.dumps({"output_dir": str(args.output_dir), "stage": results["stage"], "status": results["status"], "completed_queries": len(results["queries"]), "planned_queries": results.get("planned_queries", 0), "elapsed_seconds": results["elapsed_seconds"], "error": results.get("error")}, ensure_ascii=False))
    return 0 if results["status"]["recall_contract"] == "passed" else 1


def main(argv=None) -> int:
    """Bound native model/index work in a child process on Windows and POSIX."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--budget-worker"]:
        return _run(arguments[1:])
    args = parse_args(arguments)
    try:
        return subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--budget-worker", *arguments],
            timeout=BUDGET_SECONDS, check=False,
        ).returncode
    except subprocess.TimeoutExpired:
        failure = {"stage": "budget_exhausted", "status": "failed", "budget_seconds": BUDGET_SECONDS}
        if args.output_dir.is_dir() and (args.output_dir / "run-spec.json").is_file():
            write_json(args.output_dir / "timeout.json", failure)
        print(json.dumps(failure), file=sys.stderr)
        return 124


if __name__ == "__main__":
    raise SystemExit(main())
