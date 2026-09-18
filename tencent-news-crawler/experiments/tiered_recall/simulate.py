"""分层迁移演练：注入合成热度 -> 分区迁移 + 热度例外晋升（跑 2 轮触发连续达标）。"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path

from .heat import HeatTracker
from .ledger import TierLedger
from .migration import TierMigrationService
from .policy import TierPolicy
from . import load_store


def _seed_heat(heat: HeatTracker, meta: dict, now_ts: float, hot_fraction: float = 0.05) -> int:
    seeded = 0
    for nid in meta:
        heat.touch(nid, weight=1.0, now=now_ts - 3 * 86400)
    for nid, m in meta.items():
        if m.get("tier") == "hot":
            continue
        if int(hashlib.md5(nid.encode()).hexdigest(), 16) % 100 < hot_fraction * 100:
            heat.touch(nid, weight=12.0, now=now_ts - 86400)
            seeded += 1
    return seeded


def simulate(out_dir: str, now: date, runs: int, hot_fraction: float, tau_days: float) -> dict:
    src = Path(out_dir)
    out = src.parent / f"{src.name}_migrated"
    import shutil
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(src, out)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    hnsw = manifest["hnsw"]
    store = load_store(manifest.get("backend", "hnswlib"), out / "tiers", dim=manifest["dim"],
                       M=hnsw["M"], ef_construction=hnsw["ef_construction"], ef=hnsw["ef"])
    meta = json.loads((out / "news_meta.json").read_text(encoding="utf-8"))
    ledger = TierLedger(out / "ledger.json")
    policy = TierPolicy(**manifest["policy"])

    now_ts = float(now.toordinal()) * 86400.0
    heat = HeatTracker(tau_seconds=tau_days * 86400.0)
    seeded = _seed_heat(heat, meta, now_ts, hot_fraction)

    svc = TierMigrationService(store, ledger, policy, heat, meta)
    rounds = [svc.run(now) for _ in range(runs)]
    store.save(out / "tiers")
    ledger.save()

    all_moved = [m for r in rounds for m in r.get("moved_partitions", [])]
    all_promoted = [p for r in rounds for p in r.get("promoted", [])]
    return {
        "now": now.isoformat(),
        "seeded": seeded,
        "runs": runs,
        "rounds_detail": [
            {"moved_partitions": r.get("moved_partitions", []),
             "promoted": len(r.get("promoted", []))} for r in rounds
        ],
        "moved_partitions": all_moved,
        "promoted": len(all_promoted),
        "tier_counts": store.stats(),
        "saved_to": str(out),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", required=True)
    p.add_argument("--now", required=True)
    p.add_argument("--runs", type=int, default=2)
    p.add_argument("--hot-fraction", type=float, default=0.05)
    p.add_argument("--tau-days", type=float, default=7.0)
    a = p.parse_args()
    res = simulate(a.out_dir, date.fromisoformat(a.now), a.runs, a.hot_fraction, a.tau_days)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
