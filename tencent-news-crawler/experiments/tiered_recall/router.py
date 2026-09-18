"""分层召回路由：RRF 融合（跨层分数尺度无关），支持级联升级与全层合并。"""

from __future__ import annotations

from .policy import COLD, HOT, TIERS, WARM


class TieredRouter:
    def __init__(self, store, tiers: tuple[str, ...] = (HOT, WARM, COLD), rrf_k: int = 60,
                 tier_weights: dict[str, float] | None = None):
        self.store = store
        self.tiers = tuple(t for t in tiers if t in store.tiers)
        self.rrf_k = rrf_k
        self.tier_weights = tier_weights or {}

    def _weight(self, tier: str) -> float:
        return float(self.tier_weights.get(tier, 1.0))

    def _tier_ranked(self, tier: str, query, k: int) -> list[str]:
        ids, dists = self.store.search(tier, query, k)
        order: list[str] = []
        seen: set[str] = set()
        for nid in ids:
            if nid not in seen:
                seen.add(nid)
                order.append(nid)
        return order

    def search_all(self, query, k: int, per_tier_k: int | None = None) -> list[dict]:
        per_tier_k = per_tier_k or max(k, 50)
        scores: dict[str, float] = {}
        sources: dict[str, str] = {}
        for tier in self.tiers:
            if self.store.tiers[tier].live_count() == 0:
                continue
            for rank, nid in enumerate(self._tier_ranked(tier, query, per_tier_k), 1):
                scores[nid] = scores.get(nid, 0.0) + self._weight(tier) / (self.rrf_k + rank)
                sources.setdefault(nid, tier)
        ranked = sorted(scores, key=lambda n: scores[n], reverse=True)[:k]
        return [{"news_id": n, "score": scores[n], "tier": sources[n]} for n in ranked]

    def search_cascade(self, query, k: int, per_tier_k: int | None = None) -> list[dict]:
        per_tier_k = per_tier_k or max(k, 50)
        scores: dict[str, float] = {}
        sources: dict[str, str] = {}
        for tier in self.tiers:
            if self.store.tiers[tier].live_count() == 0:
                continue
            for rank, nid in enumerate(self._tier_ranked(tier, query, per_tier_k), 1):
                scores[nid] = scores.get(nid, 0.0) + self._weight(tier) / (self.rrf_k + rank)
                sources.setdefault(nid, tier)
            if len(scores) >= k:
                break
        ranked = sorted(scores, key=lambda n: scores[n], reverse=True)[:k]
        return [{"news_id": n, "score": scores[n], "tier": sources[n]} for n in ranked]

    def search_tier(self, tier: str, query, k: int) -> list[dict]:
        ids, dists = self.store.search(tier, query, k)
        return [{"news_id": n, "score": float(d), "tier": tier} for n, d in zip(ids, dists)]
