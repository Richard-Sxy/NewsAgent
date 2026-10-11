"""Offline news-level retrieval benchmark; never connects to enterprise services.

Run from news/: python -m experiments.local_recall_benchmark --help
Corpus text and relevance labels are loaded separately. Existing production
retrieval factories and model substitutes are deliberately not used here.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import importlib.metadata
import json
import math
from pathlib import Path
import platform
import re
import resource
import time

from .recall_dataset import load_articles_chunks, load_questions, load_snapshot_chunks
from .recall_metrics import aggregate_metrics, ann_overlap, query_metrics, unique_ranked


QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


def fingerprint(value) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def lexical_tokens(text: str) -> list[str]:
    """Explicit character/bigram baseline, not an enterprise word segmenter."""
    tokens: list[str] = []
    for part in re.findall(r"[\u4e00-\u9fff]+|[a-z0-9]+", text.casefold()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            tokens.extend(part)
            tokens.extend(part[i:i + 2] for i in range(len(part) - 1))
        else:
            tokens.append(part)
    return tokens


class BM25:
    def __init__(self, texts: list[str], *, k1: float = 1.5, b: float = 0.75):
        self.documents = [Counter(lexical_tokens(text)) for text in texts]
        self.lengths = [sum(row.values()) for row in self.documents]
        self.average_length = sum(self.lengths) / len(texts) if texts else 1.0
        frequencies = Counter(term for row in self.documents for term in row)
        self.idf = {
            term: math.log(1 + (len(texts) - df + 0.5) / (df + 0.5))
            for term, df in frequencies.items()
        }
        self.k1, self.b = k1, b

    def scores(self, query: str):
        terms = set(lexical_tokens(query))
        for document, length in zip(self.documents, self.lengths):
            score = 0.0
            for term in terms:
                count = document.get(term, 0)
                if count:
                    denominator = count + self.k1 * (
                        1 - self.b + self.b * length / (self.average_length or 1.0)
                    )
                    score += self.idf[term] * count * (self.k1 + 1) / denominator
            yield score


def news_from_scores(scores, chunks, k: int) -> list[str]:
    # Stable tie breaking is part of the experiment contract.
    order = sorted(range(len(chunks)), key=lambda i: (-float(scores[i]), chunks[i]["chunk_id"]))
    return unique_ranked((chunks[i]["news_id"] for i in order), k)


def hnsw_news(index, vector, chunks, k: int, ef: int):
    """Expand chunk candidates until K distinct news or the corpus is exhausted."""
    count = min(len(chunks), max(k * 4, k))
    while True:
        index.set_ef(max(ef, count))
        labels, _ = index.knn_query(vector, k=count, num_threads=1)
        news = unique_ranked((chunks[int(i)]["news_id"] for i in labels[0]), k)
        if len(news) >= k or count == len(chunks):
            return news, [int(i) for i in labels[0]], count
        count = min(len(chunks), count * 2)


def rrf_news(lists: list[list[str]], k: int, *, rrf_k: int = 60) -> list[str]:
    scores: dict[str, float] = {}
    for ids in lists:
        for rank, news_id in enumerate(unique_ranked(ids, len(ids) or 1), 1):
            scores[news_id] = scores.get(news_id, 0.0) + 1 / (rrf_k + rank)
    return sorted(scores, key=lambda news_id: (-scores[news_id], news_id))[:k]


def distribution(values: list[float]) -> dict:
    import numpy as np
    return {
        "samples": len(values),
        "p50_ms": float(np.percentile(values, 50)) if values else None,
        "p95_ms": float(np.percentile(values, 95)) if values else None,
        "mean_ms": sum(values) / len(values) if values else None,
    }


def run(args) -> dict:
    import hnswlib
    import numpy as np
    import torch
    from sentence_transformers import SentenceTransformer

    ks = sorted(set(args.ks))
    if not ks or any(k <= 0 for k in ks):
        raise ValueError("ks must contain positive integers")
    if args.batch_size <= 0 or args.threads <= 0 or args.max_seq_length <= 0:
        raise ValueError("batch size, threads and maximum sequence length must be positive")
    if args.hnsw_m < 2 or args.ef_construction <= 0 or args.ef_search <= 0:
        raise ValueError("invalid HNSW parameters")
    if args.articles_dir:
        chunks, provenance = load_articles_chunks(
            Path(args.articles_dir), args.chunk_chars, args.overlap
        )
    else:
        chunks, provenance = load_snapshot_chunks([Path(path) for path in args.snapshots])
    questions = load_questions([Path(path) for path in args.questions])
    if not chunks or not questions:
        raise ValueError("nonempty text corpus and labelled query set are required")
    if len({chunk["chunk_id"] for chunk in chunks}) != len(chunks):
        raise ValueError("duplicate chunk_id values")
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    if (out / "report.json").exists() and not args.overwrite:
        raise ValueError("output already has a report; choose a new directory or --overwrite")

    torch.set_num_threads(args.threads)
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    started = time.perf_counter()
    model = SentenceTransformer(
        str(Path(args.model).resolve()), device=args.device,
        local_files_only=True, trust_remote_code=False,
    )
    model.max_seq_length = args.max_seq_length
    model_load_seconds = time.perf_counter() - started
    texts = [chunk["text"] for chunk in chunks]
    query_texts = [QUERY_INSTRUCTION + row["question"] for row in questions]
    lengths = [len(tokens) for tokens in model.tokenizer(texts, truncation=False)["input_ids"]]
    query_lengths = [len(tokens) for tokens in model.tokenizer(query_texts, truncation=False)["input_ids"]]
    # Warm model inference separately; do not count initialization in warm latency.
    model.encode(["检索实验预热"], normalize_embeddings=True)
    started = time.perf_counter()
    vectors = model.encode(
        texts, batch_size=args.batch_size, normalize_embeddings=True,
        convert_to_numpy=True, show_progress_bar=True,
    ).astype("float32")
    document_embedding_seconds = time.perf_counter() - started
    query_vectors, embedding_latency = [], []
    for text in query_texts:
        started = time.perf_counter()
        query_vectors.append(model.encode([text], normalize_embeddings=True, convert_to_numpy=True)[0])
        embedding_latency.append((time.perf_counter() - started) * 1000)
    query_vectors = np.asarray(query_vectors, dtype="float32")
    if not np.isfinite(vectors).all() or not np.isfinite(query_vectors).all():
        raise ValueError("model returned non-finite embeddings")

    index = hnswlib.Index(space="ip", dim=vectors.shape[1])
    started = time.perf_counter()
    index.init_index(
        max_elements=len(chunks), ef_construction=args.ef_construction,
        M=args.hnsw_m, random_seed=args.seed,
    )
    # Single-thread construction makes the seeded index reproducible.
    index.add_items(vectors, np.arange(len(chunks)), num_threads=1)
    index_build_seconds = time.perf_counter() - started
    index.save_index(str(out / "index.bin"))
    np.save(out / "document_vectors.npy", vectors)
    np.save(out / "query_vectors.npy", query_vectors)
    bm25 = BM25(texts)
    max_k = max(ks)
    fusion_pool = max(max_k * 5, max_k)
    # Index warm-up is excluded from reported search latency.
    hnsw_news(index, query_vectors[0], chunks, max_k, args.ef_search)
    ids_in_corpus = {chunk["news_id"] for chunk in chunks}
    methods = ["bm25_character_bigram", "dense_exact", "dense_hnsw", "hybrid_rrf"]
    timings = {method: [] for method in methods}
    metric_rows = {method: [] for method in methods}
    answerable_rows = {method: [] for method in methods}
    results, overlaps, candidate_counts = [], [], []

    for question, qvec in zip(questions, query_vectors, strict=True):
        coverage = len(set(question["relevance"]) & ids_in_corpus)
        fully_answerable = coverage == len(question["relevance"])
        started = time.perf_counter()
        lexical = news_from_scores(list(bm25.scores(question["question"])), chunks, fusion_pool)
        lexical_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        exact_scores = vectors @ qvec
        exact = news_from_scores(exact_scores, chunks, max_k)
        exact_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        approximate, labels, count = hnsw_news(index, qvec, chunks, fusion_pool, args.ef_search)
        approximate_ms = (time.perf_counter() - started) * 1000
        candidate_counts.append(count)
        started = time.perf_counter()
        hybrid = rrf_news([lexical, approximate], max_k)
        fusion_ms = (time.perf_counter() - started) * 1000
        predictions = {
            "bm25_character_bigram": lexical[:max_k],
            "dense_exact": exact,
            "dense_hnsw": approximate[:max_k],
            "hybrid_rrf": hybrid,
        }
        timings["bm25_character_bigram"].append(lexical_ms)
        timings["dense_exact"].append(exact_ms)
        timings["dense_hnsw"].append(approximate_ms)
        # Local harness executes component searches serially, so add both.
        timings["hybrid_rrf"].append(lexical_ms + approximate_ms + fusion_ms)
        overlap = ann_overlap(approximate, exact, max_k)
        overlaps.append(overlap)
        chunk_k = min(max_k, len(chunks))
        exact_chunk_ids = sorted(
            range(len(chunks)), key=lambda i: (-float(exact_scores[i]), i)
        )[:chunk_k]
        chunk_overlap = len(set(labels[:chunk_k]) & set(exact_chunk_ids)) / chunk_k
        detail = {
            **question, "relevant_news_in_corpus": coverage,
            "fully_answerable": fully_answerable,
            "ann_news_overlap_at_max_k": overlap,
            "ann_chunk_overlap_at_max_k": chunk_overlap,
            "ann_candidate_chunks": count, "predictions": predictions, "metrics": {},
        }
        for method, ranking in predictions.items():
            row = query_metrics(ranking, question["relevance"], ks)
            metric_rows[method].append(row)
            if fully_answerable:
                answerable_rows[method].append(row)
            detail["metrics"][method] = row
        results.append(detail)

    limitations = [
        "Small public-news corpus is not a production traffic or full-archive sample.",
        "Existing inherited questions have incomplete, mostly single-positive relevance labels; manual independent review is pending.",
        "Queries are source-question retrieval, not a validated related-news recommendation task.",
        "Unjudged retrieved news are treated as nonrelevant; Recall/Precision/nDCG are relative to supplied labels.",
        "Latency is a single serial observation per query after model/index loading and partial warm-up; it excludes enterprise network, concurrent load and Agent execution.",
        "HNSW retrieves the fusion candidate pool before truncating to reported TopK; effective ef is max(base ef, candidate chunks), so latency is not standalone TopK service latency.",
        "Whole-process peak RSS includes the embedding model and all baseline structures, not just the HNSW index.",
    ]
    if args.snapshots:
        limitations.append("Corpus is a historical retrieved-candidate pool; selection bias can inflate recall relative to the original full corpus.")
    summary = {}
    for method in methods:
        summary[method] = {
            "all_queries": aggregate_metrics(metric_rows[method], ks),
            "fully_answerable_queries": aggregate_metrics(answerable_rows[method], ks) if answerable_rows[method] else None,
            "search_latency": distribution(timings[method]),
        }
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_rss_bytes = int(peak_rss if platform.system() == "Darwin" else peak_rss * 1024)
    model_manifest = Path(args.model) / "download_manifest.json"
    report = {
        "schema_version": "local-recall-benchmark-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corpus": {
            "news_count": len(ids_in_corpus), "chunk_count": len(chunks),
            "sha256": fingerprint(chunks), "provenance": provenance,
        },
        "queries": {
            "count": len(questions), "sha256": fingerprint(questions),
            "fully_answerable": sum(row["fully_answerable"] for row in results),
            "partly_answerable": sum(0 < row["relevant_news_in_corpus"] < len(row["relevance"]) for row in results),
            "unanswerable": sum(row["relevant_news_in_corpus"] == 0 for row in results),
            "labelled_positive_count": sum(len(row["relevance"]) for row in questions),
            "covered_positive_count": sum(row["relevant_news_in_corpus"] for row in results),
            "sources": args.questions,
        },
        "model": {
            "path": str(Path(args.model).resolve()),
            "download_manifest": json.loads(model_manifest.read_text()) if model_manifest.exists() else None,
            "dimensions": int(vectors.shape[1]), "normalized": True,
            "query_instruction": QUERY_INSTRUCTION, "max_seq_length": args.max_seq_length,
            "truncated_document_chunks": sum(length > args.max_seq_length for length in lengths),
            "truncated_queries": sum(length > args.max_seq_length for length in query_lengths),
        },
        "configuration": {key: value for key, value in vars(args).items()},
        "retrieval": {
            "reported_news_top_k": max_k,
            "ann_unique_news_candidate_limit": fusion_pool,
            "base_ef_search": args.ef_search,
            "effective_ef_search_min": max(args.ef_search, min(candidate_counts)),
            "effective_ef_search_max": max(args.ef_search, max(candidate_counts)),
            "hnsw_latency_scope": "ANN candidate recall for the fusion pool, followed by truncation to reported news TopK",
            "lexical_latency_scope": "Full lexical scan and ranking for the fusion pool",
            "exact_latency_scope": "Full exact vector scan and ranking to reported news TopK",
        },
        "environment": {
            "platform": platform.platform(), "machine": platform.machine(),
            "python": platform.python_version(),
            "packages": {name: importlib.metadata.version(name) for name in ["numpy", "torch", "sentence-transformers", "transformers", "hnswlib"]},
        },
        "performance": {
            "model_load_seconds": model_load_seconds,
            "document_embedding_seconds": document_embedding_seconds,
            "document_embedding_chunks_per_second": len(chunks) / document_embedding_seconds,
            "query_embedding_latency": distribution(embedding_latency),
            "hnsw_build_seconds": index_build_seconds,
            "hnsw_index_bytes": (out / "index.bin").stat().st_size,
            "document_vector_file_bytes": (out / "document_vectors.npy").stat().st_size,
            "document_vector_file_sha256": sha256((out / "document_vectors.npy").read_bytes()).hexdigest(),
            "query_vector_file_sha256": sha256((out / "query_vectors.npy").read_bytes()).hexdigest(),
            "hnsw_index_sha256": sha256((out / "index.bin").read_bytes()).hexdigest(),
            "whole_process_peak_rss_bytes": peak_rss_bytes,
            "hnsw_embedding_plus_search_latency": distribution([
                left + right for left, right in zip(embedding_latency, timings["dense_hnsw"], strict=True)
            ]),
            "ann_news_overlap_at_max_k": sum(overlaps) / len(overlaps),
            "ann_chunk_overlap_at_max_k": sum(row["ann_chunk_overlap_at_max_k"] for row in results) / len(results),
            "ann_candidate_chunks_min": min(candidate_counts),
            "ann_candidate_chunks_max": max(candidate_counts),
        },
        "summary": summary, "limitations": limitations, "results": results,
    }
    (out / "chunks.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in chunks) + "\n", encoding="utf-8")
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Local news retrieval benchmark", "",
        f"Corpus: {len(ids_in_corpus)} news, {len(chunks)} chunks. Queries: {len(questions)}; fully answerable: {report['queries']['fully_answerable']}.", "",
        "| Method | Hit@5 | Recall@5 | MRR@10 | nDCG@10 | Search P95 ms |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for method in methods:
        values = summary[method]["all_queries"]
        k5, k10 = min(ks, key=lambda k: abs(k - 5)), min(ks, key=lambda k: abs(k - 10))
        lines.append(f"| {method} | {values[k5]['hit_rate']:.4f} | {values[k5]['recall']:.4f} | {values[k10]['mrr']:.4f} | {values[k10]['ndcg']:.4f} | {summary[method]['search_latency']['p95_ms']:.2f} |")
    # The presentation labels must reflect non-default requested K values.
    k5, k10 = min(ks, key=lambda k: abs(k - 5)), min(ks, key=lambda k: abs(k - 10))
    lines[4] = f"| Method | Hit@{k5} | Recall@{k5} | MRR@{k10} | nDCG@{k10} | Search P95 ms |"
    lines += ["", "## Scope and limitations", "", *[f"- {item}" for item in limitations], "", "Detailed per-query rankings, hashes, performance and configuration: report.json."]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"corpus": report["corpus"]["news_count"], "chunks": len(chunks), "queries": report["queries"], "summary": summary, "report": str(out / "report.json")}, ensure_ascii=False, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--articles-dir")
    source.add_argument("--snapshots", nargs="+")
    parser.add_argument("--questions", nargs="+", required=True)
    parser.add_argument("--model", required=True, help="Downloaded local model directory; no implicit remote download")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--ks", nargs="+", type=int, default=[1, 5, 10])
    parser.add_argument("--chunk-chars", type=int, default=400)
    parser.add_argument("--overlap", type=int, default=64)
    parser.add_argument("--batch-size", type=int, default=24)
    parser.add_argument("--max-seq-length", type=int, default=512)
    parser.add_argument("--device", choices=["cpu", "mps", "cuda"], default="cpu")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--hnsw-m", type=int, default=16)
    parser.add_argument("--ef-construction", type=int, default=200)
    parser.add_argument("--ef-search", type=int, default=64)
    parser.add_argument("--overwrite", action="store_true")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
