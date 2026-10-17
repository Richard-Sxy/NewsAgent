#!/usr/bin/env python3
"""Read-only, model-free HNSW approximation audit against exact stored-vector IP.

Source-chunk vectors are engineering probes, not user questions or relevance
labels. This report cannot establish semantic retrieval quality.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import time


CORPUS = Path("data/experiments/offline_recall_fg")
SHA256 = re.compile(r"[0-9a-f]{64}")


def file_sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def verify_assets(package: Path, expected_sha256: str) -> dict[str, str]:
    """Pin the full manifest and hash all three assets actually deserialized."""
    if not SHA256.fullmatch(expected_sha256):
        raise ValueError("expected manifest SHA-256 must contain 64 lowercase hex digits")
    manifest = package / "SHA256SUMS"
    if manifest.is_symlink() or manifest.stat().st_size > 1024 * 1024:
        raise ValueError("linked or oversized manifest")
    if file_sha256(manifest) != expected_sha256:
        raise ValueError("manifest differs from the explicitly pinned SHA-256")
    entries = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](.+)", line)
        if match is None:
            raise ValueError("invalid manifest entry")
        relative = Path(match[2])
        if relative.is_absolute() or ".." in relative.parts or relative in entries:
            raise ValueError("unsafe or duplicate manifest path")
        entries[relative] = match[1].lower()
    checked = {}
    for name in ("chunks.jsonl", "embeddings.npy", "index.bin"):
        relative = CORPUS / name
        path = package / relative
        if (relative not in entries or path.is_symlink() or not path.is_file()
                or not path.resolve().is_relative_to(package.resolve())):
            raise ValueError(f"missing or unsafe required asset: {name}")
        actual = file_sha256(path)
        if actual != entries[relative]:
            raise ValueError(f"asset SHA-256 mismatch: {name}")
        checked[str(relative)] = actual
    return checked


def read_rows(path: Path, expected_rows: int) -> list[dict]:
    result = []
    with path.open("rb") as source:
        for label, raw in enumerate(source):
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("oversized metadata row")
            row = json.loads(raw)
            news_id, chunk_index, text = row.get("news_id"), row.get("chunk_index"), row.get("text")
            if (not isinstance(news_id, str) or not news_id.strip()
                    or type(chunk_index) is not int or chunk_index < 0
                    or not isinstance(text, str) or not text.strip()):
                raise ValueError("invalid chunk identity")
            result.append({"row": label, "news_id": news_id, "chunk_index": chunk_index,
                           "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    if len(result) != expected_rows:
        raise ValueError("metadata row count differs from pinned expected rows")
    return result


def sample_queries(rows: list[dict], base_rows: int, sample_size: int, seed: int) -> list[dict]:
    """Choose distinct source articles per stratum, then one chunk per article."""
    import numpy as np

    if not 0 < base_rows < len(rows) or sample_size < 2 or sample_size % 2:
        raise ValueError("require an interior base boundary and an even sample size >= 2")
    groups = {"base": defaultdict(list), "incremental": defaultdict(list)}
    for row in rows:
        groups["base" if row["row"] < base_rows else "incremental"][row["news_id"]].append(row)
    if set(groups["base"]) & set(groups["incremental"]):
        raise ValueError("a source article crosses the pinned base/new boundary")
    rng, result = np.random.default_rng(seed), []
    for stratum, articles in groups.items():
        ids = sorted(articles)
        if len(ids) < sample_size // 2:
            raise ValueError("not enough unique source articles in a stratum")
        for offset in rng.choice(len(ids), sample_size // 2, replace=False):
            candidates = articles[ids[int(offset)]]
            row = dict(candidates[int(rng.integers(len(candidates)))])
            row["stratum"] = stratum
            result.append(row)
    # Interleave strata to avoid giving one stratum all cold/late measurements.
    half = sample_size // 2
    return [row for pair in zip(result[:half], result[half:], strict=True) for row in pair]


def exact_neighbors(vectors, query, rows: list[dict], excluded_news_id: str,
                    tie_ranks, max_k: int, *, include_scores: bool = False) -> tuple[list[int], list[str], dict]:
    import numpy as np

    started = time.perf_counter()
    scores = vectors @ query
    scanned = time.perf_counter()
    eligible = np.fromiter((row["news_id"] != excluded_news_id for row in rows), dtype=bool)
    labels = np.flatnonzero(eligible)
    # The online adapter breaks equal scores using the string chunk_id suffix.
    ranked = labels[np.lexsort((tie_ranks[labels], -scores[labels]))]
    chunks, articles, seen = ranked[:max_k].tolist(), [], set()
    for label in ranked:
        news_id = rows[int(label)]["news_id"]
        if news_id not in seen:
            seen.add(news_id)
            articles.append(news_id)
            if len(articles) == max_k:
                break
    if len(chunks) != max_k or len(articles) != max_k:
        raise ValueError("insufficient non-source exact neighbors")
    timings = {"ip_scan_ms": (scanned - started) * 1000,
               "scan_and_rank_ms": (time.perf_counter() - started) * 1000}
    if include_scores:
        timings["scores"] = scores
    return chunks, articles, timings


def ann_neighbors(index, query, rows: list[dict], excluded_news_id: str, *,
                  limit: int, requested_ef: int, chunk_pool: int, threads: int):
    """Mirror BgeSnapshotVectorIndex._search pool growth, ef and article ties."""
    pool, attempts = min(max(chunk_pool, limit), len(rows)), []
    while True:
        effective_ef = max(requested_ef, pool)
        index.set_ef(effective_ef)
        labels, distances = index.knn_query(query[None, :], k=pool, num_threads=threads)
        candidates, hits = [], {}
        for raw_label, distance in zip(labels[0], distances[0], strict=True):
            label, score = int(raw_label), 1.0 - float(distance)
            if not 0 <= label < len(rows):
                raise ValueError("invalid HNSW label")
            if rows[label]["news_id"] == excluded_news_id:
                continue
            candidates.append((label, score))
            news_id = rows[label]["news_id"]
            previous = hits.get(news_id)
            if previous is None or score > previous[1]:
                hits[news_id] = (label, score)
        ranked = sorted(hits, key=lambda news_id: (-hits[news_id][1], str(hits[news_id][0])))
        attempts.append({"chunk_pool": pool, "effective_ef": effective_ef,
                         "eligible_articles": len(ranked)})
        if len(ranked) >= limit or pool == len(rows):
            candidates.sort(key=lambda pair: (-pair[1], str(pair[0])))
            return [label for label, _ in candidates[:limit]], ranked[:limit], attempts
        pool = min(pool * 2, len(rows))


def overlap_at_k(actual, expected, k: int) -> float:
    if len(expected) < k:
        raise ValueError("exact reference has fewer than k neighbors")
    return len(set(actual[:k]) & set(expected[:k])) / k


def tied_chunk_overlap(actual, expected, scores, k: int) -> float:
    """Alternative valid cutoff-tie choices count, but cannot replace better hits."""
    cutoff = scores[expected[k - 1]]
    strict_slots = sum(scores[label] > cutoff for label in expected[:k])
    strict_hits = sum(scores[label] > cutoff for label in set(actual[:k]))
    tie_hits = sum(scores[label] == cutoff for label in set(actual[:k]))
    return (strict_hits + min(tie_hits, k - strict_slots)) / k


def estimate(values: list[float], strata: list[str], *, seed: int, resamples: int) -> dict:
    """Percentile bootstrap; preserve the sampling quota within each stratum."""
    import numpy as np

    x, s = np.asarray(values, dtype=float), np.asarray(strata)
    if not len(x) or len(x) != len(s) or not np.isfinite(x).all():
        raise ValueError("invalid bootstrap observations")
    rng, samples = np.random.default_rng(seed), np.zeros(resamples)
    for group in sorted(set(strata)):
        g = x[s == group]
        samples += g[rng.integers(0, len(g), size=(resamples, len(g)))].sum(axis=1) / len(x)
    low, high = np.quantile(samples, [0.025, 0.975])
    return {"mean": float(x.mean()), "ci95": [float(low), float(high)], "n_queries": len(x)}


def latency_summary(values: list[float]) -> dict:
    import numpy as np

    return {"n": len(values), "mean_ms": float(np.mean(values)),
            "p50_ms": float(np.quantile(values, 0.5)), "p95_ms": float(np.quantile(values, 0.95)),
            "min_ms": float(min(values)), "max_ms": float(max(values))}


def evaluate(*, package: Path, expected_manifest_sha256: str, snapshot_version: str,
             expected_rows: int, expected_articles: int, dimensions: int,
             base_rows: int, expected_base_manifest_sha256: str,
             sample_size: int = 96, seed: int = 20261015, ks: tuple[int, ...] = (3, 10, 20),
             efs: tuple[int, ...] = (64, 128, 256, 512), chunk_pool: int = 200,
             threads: int = 1, warmup_queries: int = 8, bootstrap_resamples: int = 2000) -> dict:
    if (not snapshot_version.strip() or not SHA256.fullmatch(expected_base_manifest_sha256)
            or threads < 1 or chunk_pool < 1 or warmup_queries < 0 or bootstrap_resamples < 100
            or not ks or not efs or any(type(k) is not int or k < 1 for k in (*ks, *efs))
            or tuple(sorted(set(ks))) != ks or tuple(sorted(set(efs))) != efs):
        raise ValueError("invalid pinned identity or evaluation parameters")
    # Set before loading NumPy; threadpoolctl also limits a previously loaded BLAS.
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
                 "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = str(threads)
    import hnswlib
    import numpy as np
    from threadpoolctl import threadpool_info, threadpool_limits

    package = package.resolve()
    started = time.perf_counter()
    assets = verify_assets(package, expected_manifest_sha256)
    build_path = package / "build_report.json"
    build = json.loads(build_path.read_text())
    expected_build = {"status": "completed", "snapshot_version": snapshot_version,
                      "package_manifest_sha256": expected_manifest_sha256,
                      "base_manifest_sha256": expected_base_manifest_sha256,
                      "base_chunks": base_rows, "total_chunks": expected_rows,
                      "total_articles": expected_articles, "dimensions": dimensions}
    if any(build.get(key) != value for key, value in expected_build.items()):
        raise ValueError("build report differs from pinned version, identity or dimensions/counts")
    rows = read_rows(package / CORPUS / "chunks.jsonl", expected_rows)
    if len({row["news_id"] for row in rows}) != expected_articles:
        raise ValueError("article count differs from pinned expected article count")
    vectors = np.load(package / CORPUS / "embeddings.npy", mmap_mode="r", allow_pickle=False)
    if vectors.shape != (expected_rows, dimensions) or vectors.dtype != np.float32:
        raise ValueError("stored vectors differ from pinned float32 dimensions/counts")
    index = hnswlib.Index(space="ip", dim=dimensions)
    index.load_index(str(package / CORPUS / "index.bin"))
    index.set_num_threads(threads)
    if (index.get_current_count() != expected_rows
            or sorted(index.get_ids_list()) != list(range(expected_rows))):
        raise ValueError("HNSW label space differs from metadata rows")
    for offset in range(0, expected_rows, 1024):
        block = vectors[offset:offset + 1024]
        if not np.isfinite(block).all() or not np.allclose(np.linalg.norm(block, axis=1), 1, atol=0.001):
            raise ValueError("stored vectors must be finite and L2 normalized")
        if not np.allclose(index.get_items(list(range(offset, offset + len(block)))), block, atol=1e-6):
            raise ValueError("HNSW vectors differ from stored row vectors")
    samples = sample_queries(rows, base_rows, sample_size, seed)
    for sample in samples:
        sample["vector_sha256"] = hashlib.sha256(vectors[sample["row"]].tobytes()).hexdigest()
    sample_digest = hashlib.sha256(json.dumps(samples, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    tie_ranks = np.empty(expected_rows, dtype=np.int64)
    tie_ranks[sorted(range(expected_rows), key=str)] = np.arange(expected_rows)
    validation_seconds = time.perf_counter() - started
    exact, exact_scores, observations, warmups = {}, {}, [], []
    with threadpool_limits(limits=threads):
        threadpools = threadpool_info()
        # Pre-touch matrix/cache once; exclude from exact timing.
        vectors @ vectors[samples[0]["row"]]
        for sample in samples:
            query = np.asarray(vectors[sample["row"]])
            chunks, articles, timings = exact_neighbors(vectors, query, rows, sample["news_id"], tie_ranks, max(ks), include_scores=True)
            scores = timings.pop("scores")
            exact_scores[sample["row"]] = scores
            non_source = np.asarray([row["news_id"] != sample["news_id"] for row in rows])
            timings["chunk_cutoff_tie_counts"] = {str(k): int(np.count_nonzero(
                scores[non_source] == scores[chunks[k - 1]])) for k in ks}
            exact[sample["row"]] = {"chunks": chunks, "articles": articles, **timings}
        # Rotate configuration order deterministically to reduce position bias.
        configs = [(ef, k) for ef in efs for k in ks]
        np.random.default_rng(seed + 1).shuffle(configs)
        for ef, k in configs:
            for sample in samples[:warmup_queries]:
                ann_neighbors(index, np.asarray(vectors[sample["row"]]), rows, sample["news_id"],
                              limit=k, requested_ef=ef, chunk_pool=chunk_pool, threads=threads)
            warmups.append({"requested_ef": ef, "k": k, "queries": min(warmup_queries, len(samples))})
            for sample in samples:
                query = np.asarray(vectors[sample["row"]])
                tick = time.perf_counter()
                chunks, articles, attempts = ann_neighbors(index, query, rows, sample["news_id"],
                    limit=k, requested_ef=ef, chunk_pool=chunk_pool, threads=threads)
                elapsed_ms = (time.perf_counter() - tick) * 1000
                truth = exact[sample["row"]]
                observations.append({"query_row": sample["row"], "stratum": sample["stratum"],
                    "requested_ef": ef, "k": k, "latency_ms": elapsed_ms, "attempts": attempts,
                    "chunk_ann_at_k": overlap_at_k(chunks, truth["chunks"], k),
                    "chunk_ann_at_k_tie_aware": tied_chunk_overlap(chunks, truth["chunks"], exact_scores[sample["row"]], k),
                    "article_ann_at_k": overlap_at_k(articles, truth["articles"], k),
                    "ann_chunk_rows": chunks, "ann_article_ids": articles})
    summaries = []
    for ef in efs:
        for k in ks:
            found = [x for x in observations if x["requested_ef"] == ef and x["k"] == k]
            summary = {"requested_ef": ef, "k": k, "strata": {}}
            for name, group in [("all_balanced", found), *[(s, [x for x in found if x["stratum"] == s])
                                                          for s in ("base", "incremental")]]:
                summary["strata"][name] = {
                    metric: estimate([x[metric] for x in group], [x["stratum"] for x in group],
                                     seed=seed + 2, resamples=bootstrap_resamples)
                    for metric in ("chunk_ann_at_k", "chunk_ann_at_k_tie_aware", "article_ann_at_k")}
                summary["strata"][name]["latency"] = latency_summary([x["latency_ms"] for x in group])
            summary["effective_efs_observed"] = sorted({a["effective_ef"] for x in found for a in x["attempts"]})
            summary["pool_sizes_observed"] = sorted({a["chunk_pool"] for x in found for a in x["attempts"]})
            summary["expanded_queries"] = sum(len(x["attempts"]) > 1 for x in found)
            summaries.append(summary)
    for sample in samples:
        sample["exact"] = exact[sample["row"]]
    result = {
        "schema_version": 1, "status": "completed", "created_at": datetime.now(timezone.utc).isoformat(),
        "evaluation_kind": "stored_source_chunk_vector_ann_approximation_only",
        "evaluator_source_sha256": file_sha256(Path(__file__)),
        "package": str(package), "snapshot_version": snapshot_version,
        "package_manifest_sha256": expected_manifest_sha256,
        "base_manifest_sha256": expected_base_manifest_sha256,
        "build_report_sha256": file_sha256(build_path), "verified_asset_sha256": assets,
        "rows": expected_rows, "articles": expected_articles, "dimensions": dimensions,
        "repeated_news_id_chunk_index_rows": len(rows) - len({(r["news_id"], r["chunk_index"]) for r in rows}),
        "dtype": str(vectors.dtype), "base_rows": base_rows,
        "parameters": {"seed": seed, "sample_size": sample_size, "ks": list(ks), "requested_efs": list(efs),
                       "chunk_pool": chunk_pool, "threads": threads, "warmup_queries_per_configuration": warmup_queries,
                       "bootstrap_resamples": bootstrap_resamples, "bootstrap_seed": seed + 2},
        "sampling": {"method": "48/48 by default; uniform unique source articles within stratum, then one uniform chunk",
                     "stratum_boundaries": {"base": [0, base_rows], "incremental": [base_rows, expected_rows]},
                     "sample_identity_sha256": sample_digest, "query_unit": "one distinct source article per probe",
                     "aggregate_weighting": "balanced sampled strata; not corpus-population weighted"},
        "definitions": {
            "chunk_ann_at_k": "|ANN top-k non-source chunk rows intersect exact top-k non-source chunk rows| / k",
            "chunk_ann_at_k_tie_aware": "same reference; alternative rows with EXACTLY equal exact-IP cutoff scores count up to available cutoff slots; cannot replace strictly better hits",
            "article_ann_at_k": "|ANN top-k distinct non-source news_ids intersect exact max-IP-per-news_id top-k| / k",
            "source_exclusion": "exclude every chunk with the probe source news_id in both reference and ANN",
            "article_truth": "exact float32 IP over ALL stored chunks, max per news_id, then rank",
            "ties": "descending score, then lexicographic decimal row suffix of online chunk_id",
            "effective_ef": "max(requested_ef, current chunk_pool); 64/128 both become at least 200",
            "latency": "independent single query: set_ef + knn_query + source exclusion/article dedup/sort/pool retries; no embedding, async dispatch or HTTP",
            "ci95": "2000 by default, percentile bootstrap resampling source-article probes within fixed strata; conditional on this source-vector sampling design"},
        "runtime": {"python": platform.python_version(), "platform": platform.platform(),
                    "numpy": np.__version__, "hnswlib": importlib.metadata.version("hnswlib"),
                    "threadpoolctl": importlib.metadata.version("threadpoolctl"), "threadpools_during_run": threadpools,
                    "numpy_build_dependencies": getattr(np.__config__, "CONFIG", {}).get("Build Dependencies", {}),
                    "thread_control": "HNSW num_threads explicitly set; BLAS environment pinned before import plus threadpoolctl limit. An empty threadpool list means actual native BLAS thread count cannot be introspected, not that it was measured as one.",
                    "thread_environment": {name: os.environ.get(name) for name in
                        ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS")}},
        "timing": {"validation_seconds": validation_seconds, "total_seconds": time.perf_counter() - started,
                   "exact_ip_scan": latency_summary([x["ip_scan_ms"] for x in exact.values()]),
                   "exact_scan_and_ranking": latency_summary([x["scan_and_rank_ms"] for x in exact.values()])},
        "warmups": warmups, "summary": summaries, "queries": samples, "observations": observations,
        "limitations": ["Engineering ANN-neighborhood approximation, not semantic relevance/quality; no human qrels.",
                        "Queries reuse document-side source chunk vectors, not fresh user-question embeddings; BGE model is never loaded.",
                        "Repeated legacy chunk identities are retained as distinct row-based online chunk_ids; strict chunk ANN@k may penalize equally valid tied-vector neighbors, so tie-aware results are also reported.",
                        "96 probes by default, stratified by base/new package membership, not representative user demand or all news topics.",
                        "Single-thread sequential warm-cache timing; no load/concurrency/service or production latency claim.",
                        "Native BLAS thread count is not independently verified if threadpoolctl enumerates no controllable pools; see thread_environment and numpy_build_dependencies.",
                        "Bootstrap [1,1] for all-success probes is a conditional empirical interval, not proof of perfect population recall.",
                        "Manifest and three used assets verified; model weights are not loaded or independently rehashed by this audit.",
                        "Existing HNSW reused read-only; no index build, graph change, configuration activation or network access."]}
    if file_sha256(package / "SHA256SUMS") != expected_manifest_sha256:
        raise ValueError("manifest changed during evaluation")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--base-manifest-sha256", required=True)
    parser.add_argument("--snapshot-version", required=True)
    parser.add_argument("--expected-rows", type=int, default=50252)
    parser.add_argument("--expected-articles", type=int, default=20187)
    parser.add_argument("--dimensions", type=int, default=1024)
    parser.add_argument("--base-rows", type=int, default=38026)
    parser.add_argument("--sample-size", type=int, default=96)
    parser.add_argument("--seed", type=int, default=20261015)
    parser.add_argument("--ks", type=int, nargs="+", default=[3, 10, 20])
    parser.add_argument("--efs", type=int, nargs="+", default=[64, 128, 256, 512])
    parser.add_argument("--chunk-pool", type=int, default=200)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--warmup-queries", type=int, default=8)
    parser.add_argument("--bootstrap-resamples", type=int, default=2000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package, output = args.package.resolve(), args.output.resolve()
    if output.is_relative_to(package):
        raise ValueError("report must be written outside the immutable package")
    result = evaluate(package=package, expected_manifest_sha256=args.manifest_sha256,
                      expected_base_manifest_sha256=args.base_manifest_sha256,
                      snapshot_version=args.snapshot_version, expected_rows=args.expected_rows,
                      expected_articles=args.expected_articles, dimensions=args.dimensions,
                      base_rows=args.base_rows, sample_size=args.sample_size, seed=args.seed,
                      ks=tuple(args.ks), efs=tuple(args.efs), chunk_pool=args.chunk_pool,
                      threads=args.threads, warmup_queries=args.warmup_queries,
                      bootstrap_resamples=args.bootstrap_resamples)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "report": str(output), "queries": len(result["queries"]),
                      "total_seconds": result["timing"]["total_seconds"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
