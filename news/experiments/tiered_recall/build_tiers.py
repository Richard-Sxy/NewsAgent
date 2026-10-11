"""从语料构建 hot/warm/cold 三层索引与台账。

    python -m experiments.tiered_recall build --exp-dir <dir> --out-dir <dir>
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import numpy as np

from .corpus import load_chunks, max_publish, news_meta, publish_dates, rank_tier_map
from .ledger import TierLedger
from .policy import COLD, HOT, WARM, TierPolicy
from .quantize import BINARY, FP16, FP32, INT8, storage_bytes
from . import make_store

TIER_PRECISION = {HOT: FP32, WARM: INT8, COLD: BINARY}


def build(exp_dir: str, out_dir: str, policy: TierPolicy, assignment: str,
          hot_parts: int, warm_parts: int, now: date | None, store_kw: dict,
          backend: str = "hnswlib") -> dict:
    chunks = load_chunks(exp_dir)
    emb = np.load(Path(exp_dir) / "embeddings.npy")
    pubdates = publish_dates(chunks)
    now = now or max_publish(pubdates)

    if assignment == "rank":
        part_tier = rank_tier_map(pubdates, hot_parts, warm_parts)
    else:
        part_tier = {p: policy.age_tier(date.fromisoformat(f"{p}-01"), now)
                     for p in {v[:7] for v in pubdates.values()}}

    tier_of_news = {nid: part_tier[m["partition"]] for nid, m in news_meta(pubdates).items()}

    store = make_store(backend, dim=emb.shape[1], **store_kw)
    for tier, precision in TIER_PRECISION.items():
        store.init_tier(tier, precision)

    buckets: dict[str, list[int]] = {t: [] for t in TIER_PRECISION}
    for row, c in enumerate(chunks):
        buckets[tier_of_news[c["news_id"]]].append(row)

    counts = {}
    for tier, rows in buckets.items():
        if not rows:
            continue
        store.add(tier, [chunks[r]["news_id"] for r in rows], emb[rows])
        counts[tier] = len(rows)

    out = Path(out_dir)
    store.save(out / "tiers")

    meta = news_meta(pubdates)
    (out / "news_meta.json").write_text(
        json.dumps({n: {**m, "tier": tier_of_news[n]} for n, m in meta.items()},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    ledger = TierLedger(out / "ledger.json")
    for part, tier in part_tier.items():
        ledger.set_partition_tier(part, tier)
    ledger.save()

    manifest = {
        "now": now.isoformat(),
        "assignment": assignment,
        "backend": backend,
        "partitions": part_tier,
        "counts": counts,
        "precision": TIER_PRECISION,
        "storage_bytes": {t: storage_bytes(counts.get(t, 0), emb.shape[1], TIER_PRECISION[t])
                          for t in TIER_PRECISION},
        "policy": policy.__dict__,
        "hnsw": store_kw,
        "dim": int(emb.shape[1]),
        "actual_bytes": store.stats(),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    return manifest


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--exp-dir", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--backend", choices=["hnswlib", "faiss"], default="hnswlib")
    p.add_argument("--assignment", choices=["age", "rank"], default="rank")
    p.add_argument("--hot-parts", type=int, default=1)
    p.add_argument("--warm-parts", type=int, default=1)
    p.add_argument("--now", default=None)
    p.add_argument("--hot-ttl-days", type=int, default=30)
    p.add_argument("--warm-ttl-days", type=int, default=365)
    p.add_argument("--m", type=int, default=32)
    p.add_argument("--ef-construction", type=int, default=200)
    p.add_argument("--ef", type=int, default=64)
    a = p.parse_args()
    policy = TierPolicy(hot_ttl_days=a.hot_ttl_days, warm_ttl_days=a.warm_ttl_days)
    now = date.fromisoformat(a.now) if a.now else None
    m = build(a.exp_dir, a.out_dir, policy, a.assignment, a.hot_parts, a.warm_parts,
              now, {"M": a.m, "ef_construction": a.ef_construction, "ef": a.ef},
              backend=a.backend)
    print(json.dumps({"counts": m["counts"], "storage_bytes": m["storage_bytes"],
                      "partitions": m["partitions"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
