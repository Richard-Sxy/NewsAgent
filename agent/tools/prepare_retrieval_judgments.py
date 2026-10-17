#!/usr/bin/env python3
"""Prepare blind, wholly unjudged retrieval pairs from pinned public news.

No model is loaded and no relevance scores are calculated. Stored source-chunk
vectors may propose related-news candidates; they are never user-query vectors.
The files are annotation drafts, not a benchmark or a test split.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re


CORPUS = Path("data/experiments/offline_recall_fg")
SHA256 = re.compile(r"[0-9a-f]{64}")
NEWS_ID = re.compile(r"[A-Za-z0-9_-]+")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _key(seed: int, *parts: str) -> str:
    return hashlib.sha256(":".join((str(seed), *parts)).encode()).hexdigest()


def _json(path: Path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"JSON source must be an ordinary file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _pin(path: Path, expected: str) -> None:
    if not SHA256.fullmatch(expected) or file_sha256(path) != expected:
        raise ValueError(f"source SHA-256 differs from explicit pin: {path.name}")


def load_articles(manifest_path: Path, expected_sha256: str) -> tuple[dict[str, dict], dict]:
    """Validate original article bytes; retain complete bodies for judgments."""
    _pin(manifest_path, expected_sha256)
    manifest = _json(manifest_path)
    if manifest.get("schema_version") != 1 or manifest.get("timezone") != "Asia/Shanghai":
        raise ValueError("article manifest must use schema 1 and Asia/Shanghai")
    articles = {}
    for entry in manifest["articles"]:
        nid = entry.get("news_id")
        relative = Path(entry.get("relative_path", ""))
        if (not isinstance(nid, str) or not NEWS_ID.fullmatch(nid) or nid in articles
                or relative.is_absolute() or ".." in relative.parts):
            raise ValueError("invalid article identity or relative path")
        path = manifest_path.parent / relative
        if (path.is_symlink() or not path.resolve().is_relative_to(manifest_path.parent.resolve())
                or path.name != nid + ".json"):
            raise ValueError("article source escapes manifest or differs from news_id")
        _pin(path, entry["sha256"])
        article = _json(path)
        title, content = article.get("title"), article.get("content")
        if (article.get("news_id") != nid or not isinstance(title, str) or not title.strip()
                or not isinstance(content, str) or not content.strip()):
            raise ValueError("article requires a consistent identity, title and original body")
        digest = hashlib.sha256(f"{title}\x1f{content}".encode()).hexdigest()
        stamp = datetime.fromisoformat(article["publish_time"])
        if (stamp.tzinfo is None or digest != entry["document_sha256"]
                or article["publish_time"] != entry["publish_time"]):
            raise ValueError("article evidence hash or publication identity differs")
        articles[nid] = {**article, "document_sha256": digest,
                         "source_file": str(path.resolve()), "source_file_sha256": entry["sha256"]}
    if not articles:
        raise ValueError("article manifest is empty")
    return articles, manifest


def verify_index_assets(package: Path, expected_sha256: str, *, dense: bool) -> dict:
    """Read only the two pinned index assets used by this preparation job."""
    manifest = package / "SHA256SUMS"
    if manifest.is_symlink():
        raise ValueError("index manifest must not be a symbolic link")
    _pin(manifest, expected_sha256)
    entries = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](.+)", line)
        if not match:
            raise ValueError("invalid index checksum entry")
        relative = Path(match[2])
        if relative.is_absolute() or ".." in relative.parts or relative in entries:
            raise ValueError("unsafe or duplicate index checksum entry")
        entries[relative] = match[1].lower()
    hashes = {}
    for name in ("chunks.jsonl", "embeddings.npy") if dense else ("chunks.jsonl",):
        relative = CORPUS / name
        path = package / relative
        if (relative not in entries or path.is_symlink()
                or not path.resolve().is_relative_to(package.resolve())):
            raise ValueError("missing or unsafe index asset")
        _pin(path, entries[relative])
        hashes[str(relative)] = entries[relative]
    return hashes


def read_index_rows(path: Path, eligible_ids: set[str]) -> tuple[list[str], dict[str, int], int]:
    row_ids, first_rows, seen, inherited_duplicates = [], {}, set(), 0
    with path.open(encoding="utf-8") as handle:
        for label, line in enumerate(handle):
            row = json.loads(line)
            nid, chunk_index = row.get("news_id"), row.get("chunk_index")
            if (not isinstance(nid, str) or type(chunk_index) is not int or chunk_index < 0
                    or not isinstance(row.get("text"), str) or not row["text"].strip()):
                raise ValueError("invalid index row identity")
            if (nid, chunk_index) in seen:
                if nid in eligible_ids:
                    raise ValueError("duplicated eligible article chunk identity")
                # Frozen legacy rows may repeat an export-local chunk index.
                # Row labels remain distinct. These articles are outside the
                # full-body annotation universe and cannot enter any pool.
                inherited_duplicates += 1
            seen.add((nid, chunk_index))
            row_ids.append(nid)
            if nid in eligible_ids and (nid not in first_rows or chunk_index == 0):
                first_rows[nid] = label
    if set(first_rows) != eligible_ids:
        raise ValueError("an eligible original article is absent from the pinned index")
    return row_ids, first_rows, inherited_duplicates


def draft_topic(article: dict) -> str:
    """Only a sampling heuristic; these categories are not editorial labels."""
    title = article["title"]
    for topic, pattern in (
        ("technology", r"手机|苹果|iOS|芯片|人工智能|智能体|红魔|一加|机器人"),
        ("sports", r"国足|足球|篮球|NBA|皇马|维尼修斯|杨瀚森|奥运|羽毛球"),
        ("education", r"亲子|家长|孩子|招生|秋招|求职|教育|大学"),
        ("automotive", r"汽车|奔驰|尊界|宝马|销量|比亚迪"),
        ("finance", r"ETF|基金|股市|央行|营收|资金|上证"),
        ("culture", r"文旅|泉州|集章|博物馆|书院|长征|演员|剧中"),
    ):
        if re.search(pattern, title, re.I):
            return topic
    return article.get("category", "unknown")


def select_sources(articles: dict[str, dict], *, per_day: int, seed: int) -> list[str]:
    if per_day < 1:
        raise ValueError("per_day must be positive")
    dates = defaultdict(list)
    for nid, article in articles.items():
        dates[article["publish_time"][:10]].append(nid)
    result = []
    for day, ids in sorted(dates.items()):
        if len(ids) < per_day:
            raise ValueError("not enough articles for every daily source quota")
        categories = sorted({articles[nid].get("category", "unknown") for nid in ids},
                            key=lambda cat: _key(seed, day, cat))
        chosen, topics = [], set()
        for category in categories:
            candidates = sorted((nid for nid in ids if articles[nid].get("category", "unknown") == category),
                                key=lambda nid: _key(seed, nid))
            fresh = [nid for nid in candidates if draft_topic(articles[nid]) not in topics]
            if fresh:
                nid = fresh[0]
                chosen.append(nid)
                topics.add(draft_topic(articles[nid]))
            if len(chosen) == per_day:
                break
        for nid in sorted(set(ids) - set(chosen), key=lambda nid: _key(seed, nid)):
            if len(chosen) == per_day:
                break
            chosen.append(nid)
        result.extend(chosen)
    return result


def _tokens(text: str) -> set[str]:
    result = set()
    for part in re.findall(r"[\u4e00-\u9fff]+|[a-z0-9]+", text.casefold()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            result.update(part)
            result.update(part[i:i + 2] for i in range(len(part) - 1))
        else:
            result.add(part)
    return result


def lexical_candidates(query: str, articles: dict[str, dict], limit: int) -> list[tuple[str, float]]:
    q = _tokens(query)
    scored = []
    for nid, article in articles.items():
        terms = _tokens(article["title"])
        score = len(q & terms) / len(q | terms) if q | terms else 0.0
        scored.append((nid, score))
    return sorted(scored, key=lambda row: (-row[1], row[0]))[:limit]


def dense_source_candidates(vectors, row_ids: list[str], source_row: int,
                            source_id: str, eligible: dict[str, dict], limit: int) -> list[tuple[str, float]]:
    """Article-max IP to a stored source chunk; no query embedding inference."""
    import numpy as np

    scores = vectors @ vectors[source_row]
    best = {}
    for label, nid in enumerate(row_ids):
        if nid in eligible and nid != source_id:
            score = float(scores[label])
            if not math.isfinite(score):
                raise ValueError("nonfinite stored-vector candidate score")
            if score > best.get(nid, -math.inf):
                best[nid] = score
    return sorted(best.items(), key=lambda row: (-row[1], row[0]))[:limit]


def make_queries(source_ids: list[str], drafts: dict, articles: dict[str, dict], as_of: str) -> list[dict]:
    by_source = {row["seed_news_id"]: row["query"] for row in drafts["question_search"]}
    if len(by_source) != len(drafts["question_search"]) or set(by_source) != set(source_ids):
        raise ValueError("question drafts must cover each selected source exactly once")
    rows = []
    for offset, nid in enumerate(source_ids, 1):
        article = articles[nid]
        text = by_source[nid].strip()
        if not text or text == article["title"] or article["title"] in text:
            raise ValueError("question draft must not copy the source title")
        for task, query, source_id in (
            ("related_news", f"查找与这篇报道属于同一事件、能补充事实或后续进展的报道：{article['title']}", nid),
            ("question_search", text, None),
        ):
            rows.append({"query_id": f"{task}-{offset:03d}", "task": task, "query": query,
                         "source_news_id": source_id, "event_group_id": None, "split": None,
                         "status": "draft", "expected_empty": None, "as_of": as_of})
    for offset, value in enumerate(drafts.get("no_answer_candidate", []), 1):
        text = value.strip()
        if not text:
            raise ValueError("no-answer candidate query must not be blank")
        rows.append({"query_id": f"no_answer_candidate-{offset:03d}", "task": "no_answer_candidate",
                     "query": text, "source_news_id": None, "event_group_id": None, "split": None,
                     "status": "draft", "expected_empty": None, "as_of": as_of})
    if len({row["query"] for row in rows}) != len(rows):
        raise ValueError("duplicate query text")
    return rows


def merge_pool(proposals: dict[str, list[tuple[str, float | None]]], *, query_id: str,
               source_id: str | None, articles: dict[str, dict], pool_size: int, seed: int) -> list[str]:
    """Round-robin proposal types, then shuffle independently of retrieval rank."""
    selected = []
    offsets = {method: 0 for method in proposals}
    source_hash = articles[source_id]["document_sha256"] if source_id else None
    while len(selected) < pool_size:
        changed = False
        for method, candidates in proposals.items():
            while offsets[method] < len(candidates):
                nid = candidates[offsets[method]][0]
                offsets[method] += 1
                if (nid in selected or nid == source_id or nid not in articles
                        or (source_hash and articles[nid]["document_sha256"] == source_hash)):
                    continue
                selected.append(nid)
                changed = True
                break
            if len(selected) == pool_size:
                break
        if not changed:
            break
    if len(selected) != pool_size:
        raise ValueError("not enough eligible distinct articles to fill annotation pool")
    return sorted(selected, key=lambda nid: _key(seed, query_id, nid))


def _jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def prepare(*, package: Path, index_manifest_sha256: str, articles_manifest: Path,
            articles_manifest_sha256: str, drafts_path: Path, output_dir: Path,
            snapshot_version: str, seed: int = 20261015, per_day: int = 2,
            pool_size: int = 16, dense: bool = True) -> dict:
    if not snapshot_version.strip() or not 12 <= pool_size <= 20:
        raise ValueError("a snapshot version and pool_size between 12 and 20 are required")
    output_names = ("queries.jsonl", "annotation_tasks.jsonl", "evidence.jsonl",
                    "candidate_provenance.jsonl", "manifest.json")
    if any((output_dir / name).exists() for name in output_names):
        raise ValueError("annotation output already exists; choose a new directory")
    assets = verify_index_assets(package, index_manifest_sha256, dense=dense)
    articles, article_manifest = load_articles(articles_manifest, articles_manifest_sha256)
    row_ids, first_rows, inherited_duplicates = read_index_rows(
        package / CORPUS / "chunks.jsonl", set(articles))
    source_ids = select_sources(articles, per_day=per_day, seed=seed)
    drafts = _json(drafts_path)
    as_of = article_manifest["end_date"] + "T23:59:59+08:00"
    queries = make_queries(source_ids, drafts, articles, as_of)
    seed_for_query = {f"question_search-{i:03d}": nid for i, nid in enumerate(source_ids, 1)}
    vectors = None
    if dense:
        import numpy as np

        vectors = np.load(package / CORPUS / "embeddings.npy", mmap_mode="r", allow_pickle=False)
        if (vectors.ndim != 2 or len(vectors) != len(row_ids) or vectors.dtype != np.float32
                or not np.isfinite(vectors).all()):
            raise ValueError("stored vectors do not match pinned index rows")
    tasks, provenance, evidence_ids = [], [], set(source_ids)
    try:
        for query in queries:
            qid, source_id = query["query_id"], query["source_news_id"]
            seed_id = source_id or seed_for_query.get(qid)
            proposals = {}
            if source_id and vectors is not None:
                proposals["source_first_chunk_exact_ip"] = dense_source_candidates(
                    vectors, row_ids, first_rows[source_id], source_id, articles, pool_size * 3)
            if seed_id and not source_id:
                proposals["source_seed_not_a_positive_label"] = [(seed_id, None)]
            lexical_query = articles[source_id]["title"] if source_id else query["query"]
            proposals["title_character_bigram_jaccard"] = lexical_candidates(lexical_query, articles, pool_size * 3)
            if seed_id:
                topic = draft_topic(articles[seed_id])
                proposals["same_heuristic_topic_unjudged"] = [
                    (nid, None) for nid in sorted(articles, key=lambda nid: _key(seed, qid, "topic", nid))
                    if draft_topic(articles[nid]) == topic
                ][:pool_size * 3]
            proposals["random_unjudged"] = [(nid, None) for nid in sorted(
                articles, key=lambda nid: _key(seed, qid, "random", nid))][:pool_size * 3]
            pool = merge_pool(proposals, query_id=qid, source_id=source_id, articles=articles,
                              pool_size=pool_size, seed=seed)
            evidence_ids.update(pool)
            for nid in pool:
                article = articles[nid]
                tasks.append({"pair_id": f"{qid}:{nid}", "query_id": qid, "news_id": nid,
                              "task": query["task"], "query": query["query"],
                              "as_of": query["as_of"],
                              "source_news_id": source_id,
                              "source_evidence_ref": f"evidence.jsonl#{source_id}" if source_id else None,
                              "evidence_ref": f"evidence.jsonl#{nid}",
                              "evidence_sha256": article["document_sha256"],
                              "grade": None, "reviewer_id": None, "reason": None,
                              "judgment_status": "pending", "event_group_id": None, "split": None})
            provenance.append({"query_id": qid, "seed_news_id": seed_id,
                               "source_seed_not_a_positive_label": seed_id if not source_id else None,
                               "draft_author": "assistant_draft_requires_human_rewrite_or_acceptance",
                               "sampling_date": articles[seed_id]["publish_time"][:10] if seed_id else None,
                               "sampling_category": articles[seed_id].get("category") if seed_id else None,
                               "sampling_topic_heuristic": draft_topic(articles[seed_id]) if seed_id else None,
                               "query_dense_embedding_computed": False,
                               "source_vector_row": first_rows.get(source_id) if source_id else None,
                               "candidates": [{"news_id": nid, "proposals": [
                                   {"method": method, "score": score, "proposal_rank": rank}
                                   for method, candidates in proposals.items()
                                   for rank, (candidate, score) in enumerate(candidates, 1) if candidate == nid
                               ]} for nid in pool]})
    finally:
        if vectors is not None and getattr(vectors, "_mmap", None) is not None:
            vectors._mmap.close()
    evidence = [{"news_id": nid, "title": articles[nid]["title"],
                 "content": articles[nid]["content"], "source_url": articles[nid]["url"],
                 "publish_time": articles[nid]["publish_time"],
                 "content_version": articles[nid]["content_version"],
                 "document_sha256": articles[nid]["document_sha256"],
                 "source_file": articles[nid]["source_file"],
                 "source_file_sha256": articles[nid]["source_file_sha256"],
                 "evidence_is_untrusted": True} for nid in sorted(evidence_ids)]
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in (("queries.jsonl", queries), ("annotation_tasks.jsonl", tasks),
                       ("evidence.jsonl", evidence), ("candidate_provenance.jsonl", provenance)):
        _jsonl(output_dir / name, rows)
    manifest = {
        "schema_version": "retrieval-annotation-draft-v1", "status": "pending_human_annotation",
        "generated_at": datetime.now(timezone.utc).isoformat(), "snapshot_version": snapshot_version,
        "index_package": str(package.resolve()), "index_manifest_sha256": index_manifest_sha256,
        "index_assets_sha256": assets, "index_rows": len(row_ids), "index_articles": len(set(row_ids)),
        "index_total": len(set(row_ids)), "source_verified_full_body": len(articles),
        "inherited_noneligible_duplicate_news_chunk_indices": inherited_duplicates,
        "annotation_pool_scope": "source_manifest_only", "eligible_original_articles": len(articles),
        "articles_manifest": str(articles_manifest.resolve()),
        "articles_manifest_sha256": articles_manifest_sha256,
        "draft_input": str(drafts_path.resolve()), "draft_input_sha256": file_sha256(drafts_path),
        "query_counts": dict(Counter(row["task"] for row in queries)), "query_count": len(queries),
        "source_counts_by_date": dict(Counter(articles[nid]["publish_time"][:10] for nid in source_ids)),
        "annotation_pairs": len(tasks), "full_article_evidence_count": len(evidence),
        "pool_size_per_query": pool_size, "random_seed": seed, "source_quota_per_day": per_day,
        "as_of": as_of, "all_grades_are_null": True, "all_reviewers_are_null": True,
        "test_split_created": False, "semantic_quality_score_computed": False,
        "query_dense_embeddings_computed": False, "source_vector_candidate_proposals_enabled": dense,
        "limitations": [
            "Draft questions are source-informed assistant drafts, not real user traffic or independent gold queries.",
            "All grades, reviewers, expected_empty, event groups and splits remain unassigned; do not score these files.",
            "A source seed and stored-source-vector similarity are candidate-generation provenance, never relevance truth.",
            "Candidate pools are limited to the verified original-article manifest, a subset of the full index.",
            "Inherited noneligible index rows may repeat export-local (news_id,chunk_index); row labels remain distinct and those articles are excluded from annotation pools.",
            "Pools are incomplete; random/topic candidates are unjudged and are not certified negatives.",
            "Related tasks exclude every chunk of the same news_id and exact source document hashes; near-duplicate source copies still need review.",
            "No-answer candidates require human search and corpus coverage confirmation; none is declared unanswerable.",
            "Two independent blinded judgments, adjudication, event grouping and temporal split freezing are still required.",
            "Publication and contents reproduce public source records; annotation must check contradictions and eligibility at as_of.",
        ],
        "output_sha256": {name: file_sha256(output_dir / name) for name in output_names if name != "manifest.json"},
    }
    with (output_dir / "manifest.json").open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--index-manifest-sha256", required=True)
    parser.add_argument("--articles-manifest", type=Path, required=True)
    parser.add_argument("--articles-manifest-sha256", required=True)
    parser.add_argument("--drafts", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--snapshot-version", required=True)
    parser.add_argument("--seed", type=int, default=20261015)
    parser.add_argument("--per-day", type=int, default=2)
    parser.add_argument("--pool-size", type=int, default=16)
    parser.add_argument("--skip-dense", action="store_true")
    args = parser.parse_args()
    result = prepare(package=args.package, index_manifest_sha256=args.index_manifest_sha256,
                     articles_manifest=args.articles_manifest, articles_manifest_sha256=args.articles_manifest_sha256,
                     drafts_path=args.drafts, output_dir=args.output_dir, snapshot_version=args.snapshot_version,
                     seed=args.seed, per_day=args.per_day, pool_size=args.pool_size, dense=not args.skip_dense)
    print(json.dumps({"manifest": str(args.output_dir / "manifest.json"), "queries": result["query_counts"],
                      "annotation_pairs": result["annotation_pairs"], "output_sha256": result["output_sha256"]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
