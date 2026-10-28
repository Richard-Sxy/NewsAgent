"""Independent read-only reconstruction of the human pooled retrieval metrics.

Uses the completed SQLite backup, the original frozen labels and saved rankings.
No model, network, semantic relabelling or modification of source judgments.
"""
from pathlib import Path
import collections
import hashlib
import json
import math
import sqlite3
import statistics

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
COMPARISON = ROOT / "output/news-events-annual-20261027/comparison-v1/comparison.json"
EXPORT = ROOT / "output/news-events-annual-20261027/human-review-v1/human-review.export.json"
SCORED = ROOT / "output/news-events-annual-20261027/comparison-v1/results.json"

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def verify():
    comp = json.loads(COMPARISON.read_text(encoding="utf-8"))
    export = json.loads(EXPORT.read_text(encoding="utf-8"))
    labels = {}
    origins = collections.Counter()
    for run in export["runs"]:
        for candidate in run["candidates"]:
            assert candidate["judgment"]["status"] == "judged"
            labels[run["query_id"], candidate["news_id"]] = candidate["judgment"]["label"]
            origins["original:" + candidate["judgment"]["label"]] += 1
    with sqlite3.connect((OUT / "supplemental.completed.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        assert db.execute("pragma quick_check").fetchone()[0] == "ok"
        rows = db.execute("select r.payload,j.news_id,j.label from lab_runs r join lab_judgments j on r.run_id=j.run_id").fetchall()
    for payload, news_id, label in rows:
        run = json.loads(payload)
        key = run["query_id"], news_id
        assert key not in labels
        labels[key] = label
        origins["supplement:" + label] += 1
    grade = {"irrelevant": 0, "partial": 1, "relevant": 2}
    scores = collections.defaultdict(list)
    precision = collections.defaultdict(list)
    counts = collections.Counter()
    per_query = []
    pairs = 0
    for query in comp["queries"]:
        query_id = query["query_id"]
        pool = {p["news_id"] for seq in query["predictions"].values() for p in seq}
        pairs += len(pool)
        values = {n: grade[labels[query_id, n]] for n in pool}
        ideal = sum((2 ** v - 1) / math.log2(i + 2) for i, v in enumerate(sorted(values.values(), reverse=True)[:5]))
        assert ideal > 0
        local = {}
        for arm in ("legacy_rrf", "global_similarity"):
            ranking = query["predictions"][arm][:5]
            assert len(ranking) == 5 and len({p["news_id"] for p in ranking}) == 5
            grades = [values[p["news_id"]] for p in ranking]
            observed = sum((2 ** v - 1) / math.log2(i + 2) for i, v in enumerate(grades))
            local[arm] = observed / ideal
            scores[arm].append(local[arm])
            precision[arm].append(sum(v == 2 for v in grades) / 5)
        delta = local["global_similarity"] - local["legacy_rrf"]
        counts["improved" if delta > 1e-12 else "worsened" if delta < -1e-12 else "unchanged"] += 1
        per_query.append({"query_id": query_id, "category": query["category"], "ndcg5": local, "delta": delta})
    expected = json.loads(SCORED.read_text(encoding="utf-8"))
    metrics = {}
    for arm in scores:
        metrics[arm] = {"ndcg_at_5": statistics.mean(scores[arm]), "precision_at_5": statistics.mean(precision[arm]), "query_denominator": len(scores[arm])}
        assert math.isclose(metrics[arm]["ndcg_at_5"], expected["metrics"][arm]["5"]["pooled_ndcg"], abs_tol=1e-12)
        assert math.isclose(metrics[arm]["precision_at_5"], expected["metrics"][arm]["5"]["precision"], abs_tol=1e-12)
    assert pairs == 454 and len(comp["queries"]) == 30 and len(labels) == 454
    result = {"status": "independently_recomputed", "scope": "single-human development pooled fusion ablation",
        "formal_independent_test": False, "queries": 30, "pooled_pairs": pairs,
        "label_distribution": dict(origins), "metrics": metrics, "query_changes": dict(counts),
        "ndcg_absolute_gain": metrics["global_similarity"]["ndcg_at_5"] - metrics["legacy_rrf"]["ndcg_at_5"],
        "precision_gain_percentage_points": (metrics["global_similarity"]["precision_at_5"] - metrics["legacy_rrf"]["precision_at_5"]) * 100,
        "per_query": per_query,
        "evidence_sha256": {str(p.relative_to(ROOT)): digest(p) for p in
            (COMPARISON, EXPORT, SCORED, OUT / "supplemental.completed.sqlite3", Path(__file__).resolve())},
        "limitations": ["30 development questions, not independent test", "154 supplementary judgments are all irrelevant; recommend human spot-check",
            "local example RRF versus global cosine only; no entity/event reranking", "7/90-day frozen corpus, separate from 7/30/365 capacity workload"]}
    target = OUT / "verification.json"
    with target.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({k: v for k, v in result.items() if k not in ("per_query", "evidence_sha256")}, ensure_ascii=False))

if __name__ == "__main__":
    verify()
