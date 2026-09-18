"""多层召回评测：hot / warm / cold / all / cascade 各模式 recall@k 与 MRR。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer

from .corpus import news_meta, publish_dates
from .policy import COLD, HOT, WARM
from .router import TieredRouter
from . import load_store

DEFAULT_MODEL = str(Path(__file__).resolve().parents[2] / "data" / "models" / "bge-large-zh-v1.5")
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


def _nid(url: str) -> str:
    import re
    return re.sub(r"[?#].*$", "", url or "").rstrip("/").split("/")[-1]


def evaluate(out_dir: str, questions_path: str, model_path: str, device: str,
             topk: int, batch_size: int, exp_dir: str | None,
             tier_weights: dict[str, float] | None = None) -> dict:
    out = Path(out_dir)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    hnsw = manifest["hnsw"]
    store = load_store(manifest.get("backend", "hnswlib"), out / "tiers", dim=manifest["dim"],
                       M=hnsw["M"], ef_construction=hnsw["ef_construction"], ef=hnsw["ef"])
    router = TieredRouter(store, tier_weights=tier_weights)
    meta = json.loads((out / "news_meta.json").read_text(encoding="utf-8"))

    questions = json.loads(Path(questions_path).read_text(encoding="utf-8"))
    model = SentenceTransformer(model_path, device=device)
    model.max_seq_length = 512
    qvecs = model.encode([QUERY_INSTRUCTION + q["question"] for q in questions],
                         batch_size=batch_size, normalize_embeddings=True,
                         convert_to_numpy=True).astype("float32")

    modes = {
        "hot_only": lambda q: router.search_tier(HOT, q, topk),
        "warm_only": lambda q: router.search_tier(WARM, q, topk),
        "cold_only": lambda q: router.search_tier(COLD, q, topk),
        "all_tiers": lambda q: router.search_all(q, topk),
        "cascade": lambda q: router.search_cascade(q, topk),
    }
    results = {m: [] for m in modes}
    for q, qv in zip(questions, qvecs):
        expected = {_nid(u) for u in (q.get("expected_urls") or [])}
        for m, fn in modes.items():
            hits = fn(qv)
            got, seen = [], set()
            for h in hits:
                if h["news_id"] not in seen:
                    seen.add(h["news_id"])
                    got.append(h["news_id"])
            rank = next((r for r, g in enumerate(got, 1) if g in expected), 0)
            results[m].append({"id": q.get("id"), "expected": sorted(expected),
                               "got": got[:5], "rank": rank,
                               "top_tier": hits[0]["tier"] if hits else None})

    n = len(questions)

    def recall(rows, k):
        return sum(1 for r in rows if 0 < r["rank"] <= k) / n

    def mrr(rows):
        return sum((1.0 / r["rank"]) for r in rows if r["rank"]) / n

    summary = {m: {"recall@1": recall(rows, 1), "recall@5": recall(rows, 5),
                   "recall@10": recall(rows, 10), "MRR": mrr(rows)}
               for m, rows in results.items()}

    tier_of_expected = {}
    for q in questions:
        for u in (q.get("expected_urls") or []):
            tier_of_expected[q.get("id")] = meta.get(_nid(u), {}).get("tier")
    return {"summary": summary, "results": results, "tier_of_expected": tier_of_expected,
            "counts": store.stats()}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", required=True)
    p.add_argument("--questions", required=True)
    p.add_argument("--exp-dir", default=None)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--device", default="cuda")
    p.add_argument("--topk", type=int, default=10)
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--tier-weights", default="", help="如 hot=1,warm=1,cold=1")
    a = p.parse_args()
    weights = {}
    for part in filter(None, a.tier_weights.split(",")):
        k, v = part.split("=")
        weights[k.strip()] = float(v)
    res = evaluate(a.out_dir, a.questions, a.model, a.device, a.topk, a.batch_size,
                   a.exp_dir, weights or None)
    print(f"tier counts: {json.dumps(res['counts'], ensure_ascii=False)}")
    print(f"{'mode':<10} {'@1':>6} {'@5':>6} {'@10':>6} {'MRR':>6}")
    for m, s in res["summary"].items():
        print(f"{m:<10} {s['recall@1']:.3f} {s['recall@5']:.3f} {s['recall@10']:.3f} {s['MRR']:.3f}")
    from collections import Counter
    print("expected tier distribution:",
          dict(Counter(v for v in res["tier_of_expected"].values())))


if __name__ == "__main__":
    main()
