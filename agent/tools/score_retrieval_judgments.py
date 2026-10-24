"""Score frozen, independently judged news retrieval runs; never invent labels.

This offline tool has no application settings, network, model or database calls.
Draft queries, unjudged returned documents, missing queries and incomplete review
block scoring. Recall/nDCG are explicitly scoped to the judged pool, not all news.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import statistics


class EvaluationBlocked(ValueError):
    pass


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise EvaluationBlocked("JSONL rows must be objects")
                rows.append(row)
    return rows


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise EvaluationBlocked(f"missing nonempty {name}")
    return value


def _grade(value) -> int:
    if type(value) is not int or not 0 <= value <= 3:
        raise EvaluationBlocked("grades must be integers in [0,3]")
    return value


def _by_query(rows: list[dict], name: str) -> dict[str, dict]:
    out = {}
    for row in rows:
        qid = _text(row.get("query_id"), "query_id")
        if qid in out:
            raise EvaluationBlocked(f"duplicate {name} query_id: {qid}")
        out[qid] = row
    if not out:
        raise EvaluationBlocked(f"empty {name}")
    return out


def cluster_interval(rows: list[dict], metric: str, *, seed: int, samples: int = 2000) -> dict:
    """Resample event groups, preserving within-event correlation."""
    groups = defaultdict(list)
    for row in rows:
        groups[row["event_group_id"]].append(row[metric])
    keys = sorted(groups)
    mean = statistics.mean(row[metric] for row in rows)
    if len(keys) < 2:
        return {"mean": mean, "ci95": None, "event_groups": len(keys),
                "ci_note": "At least two independent event groups are needed."}
    rng = random.Random(seed)
    means = []
    for _ in range(samples):
        selected = [value for _ in keys for value in groups[rng.choice(keys)]]
        means.append(statistics.mean(selected))
    means.sort()
    return {"mean": mean, "ci95": [means[int(.025 * samples)], means[int(.975 * samples)]],
            "event_groups": len(keys)}


def score(queries: list[dict], qrels: list[dict], predictions: list[dict], *,
          package_sha256: str, split: str = "test", ks: tuple[int, ...] = (3, 10, 20),
          seed: int = 20261015) -> dict:
    if not ks or any(type(k) is not int or k <= 0 for k in ks):
        raise EvaluationBlocked("cutoffs must be positive integers")
    if len(ks) != len(set(ks)):
        raise EvaluationBlocked("cutoffs must be distinct")
    if len(package_sha256) != 64 or any(c not in "0123456789abcdef" for c in package_sha256):
        raise EvaluationBlocked("package_sha256 must be a pinned SHA-256")
    if split not in {"development", "validation", "test"}:
        raise EvaluationBlocked("invalid split")
    all_queries = _by_query(queries, "queries")
    event_splits = {}
    for row in all_queries.values():
        if row.get("status") != "frozen" or row.get("human_reviewed") is not True:
            raise EvaluationBlocked("draft/unreviewed queries cannot be scored")
        _text(row.get("query"), "approved query text")
        if row.get("task") not in {"related_news", "question_search", "no_answer_candidate"}:
            raise EvaluationBlocked("unknown retrieval task")
        if row["task"] == "related_news":
            _text(row.get("source_news_id"), "related-news source_news_id")
        elif row.get("source_news_id") is not None:
            raise EvaluationBlocked("question-source seeds belong in hidden provenance, not exclusions")
        event = _text(row.get("event_group_id"), "human assigned event_group_id")
        row_split = row.get("split")
        if row_split not in {"development", "validation", "test"}:
            raise EvaluationBlocked("every query needs a frozen split")
        if event in event_splits and event_splits[event] != row_split:
            raise EvaluationBlocked("same event group leaks across splits")
        event_splits[event] = row_split
        if type(row.get("expected_empty")) is not bool:
            raise EvaluationBlocked("expected_empty needs a human corpus-scope decision")
        if row.get("judgment_scope") != "pooled":
            raise EvaluationBlocked("this scorer requires explicit pooled judgment scope")
    selected = {qid: row for qid, row in all_queries.items() if row["split"] == split}
    if not selected:
        raise EvaluationBlocked("requested split has no queries")
    preds = _by_query(predictions, "predictions")
    if set(preds) != set(selected):
        raise EvaluationBlocked("missing/extra predictions: denominator cannot change")
    labels = defaultdict(dict)
    for row in qrels:
        qid = _text(row.get("query_id"), "qrels query_id")
        nid = _text(row.get("news_id"), "qrels news_id")
        if qid not in all_queries or nid in labels[qid]:
            raise EvaluationBlocked("unknown query or duplicate qrel")
        if all_queries[qid].get("source_news_id") == nid:
            raise EvaluationBlocked("source article must not enter the relevance denominator")
        if row.get("status") != "adjudicated":
            raise EvaluationBlocked("all qrels require completed independent review")
        votes = row.get("judgments")
        if not isinstance(votes, list) or len(votes) < 2:
            raise EvaluationBlocked("at least two independent reviewer judgments are required")
        reviewers, grades = set(), []
        for vote in votes:
            reviewer = _text(vote.get("reviewer_id"), "reviewer_id")
            if reviewer in reviewers:
                raise EvaluationBlocked("reviewers must be distinct")
            reviewers.add(reviewer)
            grades.append(_grade(vote.get("grade")))
        final = _grade(row.get("final_grade"))
        if len(set(grades)) > 1:
            adjudicator = _text(row.get("adjudicator_id"), "disagreement adjudicator_id")
            if adjudicator in reviewers:
                raise EvaluationBlocked("disagreement needs a separate adjudicator")
        elif final != grades[0]:
            raise EvaluationBlocked("unanimous grades cannot silently change")
        labels[qid][nid] = final
    metrics, negatives = [], []
    for qid, query in selected.items():
        pred = preds[qid]
        if pred.get("package_manifest_sha256") != package_sha256:
            raise EvaluationBlocked("prediction package identity drift")
        ranked = pred.get("news_ids")
        if not isinstance(ranked, list) or any(not isinstance(n, str) or not n.strip() or n != n.strip() for n in ranked):
            raise EvaluationBlocked("predictions require ranked news_ids")
        if len(ranked) != len(set(ranked)):
            raise EvaluationBlocked("predictions must already be article-deduplicated")
        if query.get("source_news_id") in ranked:
            raise EvaluationBlocked("source article leaked into related-news predictions")
        grades = labels.get(qid, {})
        if not grades or any(nid not in grades for nid in ranked[:max(ks)]):
            raise EvaluationBlocked("missing qrels or unjudged top-K; do not treat unknown as irrelevant")
        base = {"query_id": qid, "task": query["task"], "event_group_id": query["event_group_id"]}
        positives = {nid for nid, grade in grades.items() if grade >= 2}
        if query["expected_empty"]:
            if query.get("corpus_no_answer_reviewed") is not True:
                raise EvaluationBlocked("no-answer needs a human corpus-scope review")
            if positives:
                raise EvaluationBlocked("no-answer judgment conflicts with positive graded evidence")
            negatives.append({**base, "false_positive": float(bool(ranked)),
                              "correct_abstention": float(not ranked)})
            continue
        if not positives:
            raise EvaluationBlocked("answerable query has no grade>=2 evidence")
        row = dict(base)
        ideal = sorted((2 ** grade - 1 for grade in grades.values()), reverse=True)
        for k in ks:
            top = ranked[:k]
            hits = len(set(top) & positives)
            row[f"pooled_recall@{k}"] = hits / len(positives)
            row[f"precision@{k}"] = hits / k
            row[f"hit_rate@{k}"] = float(hits > 0)
            first = next((i for i, nid in enumerate(top, 1) if nid in positives), None)
            row[f"mrr@{k}"] = 1 / first if first else 0.0
            dcg = math.fsum((2 ** grades[nid] - 1) / math.log2(i + 1) for i, nid in enumerate(top, 1))
            idcg = math.fsum(gain / math.log2(i + 1) for i, gain in enumerate(ideal[:k], 1))
            row[f"pooled_ndcg_exp2@{k}"] = dcg / idcg
        metrics.append(row)
    names = [key for key in metrics[0] if "@" in key] if metrics else []
    aggregate = {name: cluster_interval(metrics, name, seed=seed) for name in names}
    strata = {}
    for task in sorted({row["task"] for row in metrics}):
        rows = [row for row in metrics if row["task"] == task]
        strata[task] = {"queries": len(rows), "metrics": {n: cluster_interval(rows, n, seed=seed) for n in names}}
    return {"schema_version": 1, "status": "scored", "acceptance_status": "not_approved",
            "package_manifest_sha256": package_sha256, "split": split, "seed": seed,
            "denominator": len(selected), "answerable_queries": len(metrics), "no_answer_queries": len(negatives),
            "metrics": aggregate, "by_task": strata,
            "no_answer": {n: cluster_interval(negatives, n, seed=seed) for n in ("false_positive", "correct_abstention")} if negatives else None,
            "per_query": metrics + negatives,
            "limitations": ["No automatic production acceptance; proposed gates need human approval.",
                            "Recall/nDCG denominators cover the judged pool, not all relevant corpus articles.",
                            "95% percentile bootstrap resamples event groups; small group counts remain weak evidence.",
                            "This scorer validates judgments; corpus membership, time, evidence hashes and pool completeness require dataset freeze audit."]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("queries", "qrels", "predictions", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("queries", "qrels", "predictions"):
        parser.add_argument("--expected-" + name + "-sha256", required=True)
    parser.add_argument("--package-sha256", required=True)
    parser.add_argument("--split", choices=("development", "validation", "test"), default="test")
    parser.add_argument("--ks", nargs="+", type=int, default=(3, 10, 20),
                        metavar="K", help="Distinct positive ranking cutoffs (default: 3 10 20)")
    args = parser.parse_args()
    identity = {}
    try:
        for name in ("queries", "qrels", "predictions"):
            digest = sha256(getattr(args, name))
            if digest != getattr(args, "expected_" + name + "_sha256"):
                raise EvaluationBlocked(f"{name} file hash differs from preregistered input")
            identity[name + "_sha256"] = digest
        report = score(load_jsonl(args.queries), load_jsonl(args.qrels), load_jsonl(args.predictions),
                       package_sha256=args.package_sha256, split=args.split, ks=tuple(args.ks))
    except (EvaluationBlocked, ValueError, OSError, KeyError, TypeError) as exc:
        report = {"schema_version": 1, "status": "blocked", "acceptance_status": "not_approved",
                  "reason": str(exc), "metrics": None}
    report["inputs"] = identity
    report["created_at"] = datetime.now(timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if report["status"] == "scored" else 2


if __name__ == "__main__":
    raise SystemExit(main())
