"""分层迁移：分区整块迁移 + 热度例外晋升。幂等，先写后删。"""

from __future__ import annotations

from datetime import date

from .policy import COLD, HOT, TIERS, WARM, TierPolicy


class TierMigrationService:
    def __init__(self, store, ledger, policy: TierPolicy, heat, news_meta: dict):
        self.store = store
        self.ledger = ledger
        self.policy = policy
        self.heat = heat
        self.news_meta = news_meta
        self.partitions: dict[str, list[str]] = {}
        for nid, m in news_meta.items():
            self.partitions.setdefault(m["partition"], []).append(nid)

    def _partition_date(self, partition: str) -> date:
        return date.fromisoformat(f"{partition}-01")

    def boundary_partitions(self, now: date) -> list[dict]:
        out = []
        for partition in sorted(self.partitions):
            current = self.ledger.get_partition_tier(partition)
            if current is None:
                continue
            target = self.policy.age_tier(self._partition_date(partition), now)
            if current != target:
                out.append({"partition": partition, "from": current, "to": target})
        return out

    def _move(self, ids: list[str], src: str, dst: str) -> int:
        if hasattr(self.store, "move"):
            return self.store.move(src, dst, ids)
        vecs, kept = self.store.vectors_of(src, ids)
        if not kept:
            return 0
        self.store.add(dst, kept, vecs)
        self.store.delete(src, kept)
        return len(kept)

    def migrate_partitions(self, now: date) -> dict:
        moved = []
        for b in self.boundary_partitions(now):
            ids = self.partitions[b["partition"]]
            n = self._move(ids, b["from"], b["to"])
            if n:
                self.ledger.set_partition_tier(b["partition"], b["to"])
                self.ledger.record({"type": "partition", "partition": b["partition"],
                                    "from": b["from"], "to": b["to"], "vectors": n})
                moved.append({**b, "vectors": n})
        return {"moved_partitions": moved}

    def current_tier(self, news_id: str) -> str:
        return (self.ledger.get_vector_tier(news_id)
                or self.ledger.get_partition_tier(self.news_meta[news_id]["partition"])
                or WARM)

    def promote_exceptions(self, now: date) -> dict:
        promoted = []
        for nid, meta in self.news_meta.items():
            current = self.current_tier(nid)
            heat = self.heat.get(nid, now=float(now.toordinal()) * 86400.0)
            streak = self.ledger.get_streak(nid)
            target, new_streak = self.policy.decide(
                publish=date.fromisoformat(meta["publish"]), now=now,
                heat=heat, current=current, streak=streak)
            self.ledger.set_streak(nid, new_streak)
            if target != current:
                n = self._move([nid], current, target)
                if n:
                    self.ledger.set_vector_tier(nid, target)
                    self.ledger.record({"type": "exception", "news_id": nid,
                                        "from": current, "to": target,
                                        "heat": round(heat, 3), "vectors": n})
                    promoted.append({"news_id": nid, "from": current, "to": target,
                                     "heat": round(heat, 3)})
        return {"promoted": promoted}

    def run(self, now: date) -> dict:
        result = self.migrate_partitions(now)
        result.update(self.promote_exceptions(now))
        self.ledger.save()
        return result
