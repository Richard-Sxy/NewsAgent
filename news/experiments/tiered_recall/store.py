"""三层向量存储：每层一个 hnswlib 索引，按层精度量化，支持增删与持久化。"""

from __future__ import annotations

import json
from pathlib import Path

import hnswlib
import numpy as np

from .quantize import PRECISIONS, storage_bytes

"""  """
class _Tier:
    def __init__(self, tier: str, dim: int, precision: str, M: int, ef_construction: int, ef: int):
        self.tier = tier
        self.dim = dim
        self.precision = precision
        self.ef = ef
        self.ids: list[str] = []
        self.vectors = np.zeros((0, dim), dtype=np.float32)
        self.index = hnswlib.Index(space="ip", dim=dim)
        self._capacity = 1024
        self.index.init_index(max_elements=self._capacity,
                              ef_construction=ef_construction, M=M)
        self.index.set_ef(ef)
        self.rows: dict[str, list[int]] = {}

    def _ensure(self, extra: int) -> None:
        need = len(self.ids) + extra
        if need <= self._capacity:
            return
        while self._capacity < need:
            self._capacity *= 2
        self.index.resize_index(self._capacity)

    def add(self, ids: list[str], vectors: np.ndarray) -> int:
        self._ensure(len(ids))
        start = len(self.ids)
        self.index.add_items(vectors.astype(np.float32), np.arange(start, start + len(ids)))
        self.vectors = np.vstack([self.vectors, vectors.astype(np.float32)])
        self.ids.extend(ids)
        for offset, nid in enumerate(ids):
            self.rows.setdefault(nid, []).append(start + offset)
        return len(ids)

    def live_rows(self, ids: list[str]) -> tuple[list[int], list[str]]:
        rows, kept = [], []
        for nid in ids:
            for r in self.rows.get(nid, []):
                if r in self._deleted:
                    continue
                rows.append(r)
                kept.append(nid)
                break
        return rows, kept

    def delete(self, ids: list[str]) -> int:
        rows, _ = self.live_rows(ids)
        for r in rows:
            self.index.mark_deleted(int(r))
            self._deleted.add(r)
        return len(rows)

    @property
    def _deleted(self) -> set[int]:
        if not hasattr(self, "_deleted_set"):
            self._deleted_set: set[int] = set()
        return self._deleted_set

    def search(self, query: np.ndarray, k: int):
        n = min(k, len(self.ids))
        if n <= 0:
            return [], []
        labels, dists = self.index.knn_query(query.astype(np.float32), k=n)
        return labels[0].tolist(), dists[0].tolist()

    def live_count(self) -> int:
        return len(self.ids) - len(self._deleted)

    def bytes_used(self) -> int:
        return storage_bytes(len(self.ids), self.dim, self.precision)

""""""
class TierStore:
    def __init__(self, dim: int, M: int = 32, ef_construction: int = 200, ef: int = 64):
        self.dim = dim
        self.M = M
        self.ef_construction = ef_construction
        self.ef = ef
        self.tiers: dict[str, _Tier] = {}

    def init_tier(self, tier: str, precision: str) -> None:
        if precision not in PRECISIONS:
            raise ValueError(f"unknown precision: {precision}")
        self.tiers[tier] = _Tier(tier, self.dim, precision, self.M, self.ef_construction, self.ef)

    def add(self, tier: str, ids: list[str], vectors: np.ndarray) -> int:
        from .quantize import roundtrip
        return self.tiers[tier].add(ids, roundtrip(vectors, self.tiers[tier].precision))

    def delete(self, tier: str, ids: list[str]) -> int:
        return self.tiers[tier].delete(ids)

    def search(self, tier: str, query: np.ndarray, k: int):
        labels, dists = self.tiers[tier].search(query, k)
        t = self.tiers[tier]
        return [t.ids[i] for i in labels], dists

    def vectors_of(self, tier: str, ids: list[str]) -> tuple[np.ndarray, list[str]]:
        rows, kept = self.tiers[tier].live_rows(ids)
        if not rows:
            return np.zeros((0, self.dim), dtype=np.float32), []
        return self.tiers[tier].vectors[rows], kept

    def stats(self) -> dict:
        return {
            t: {"precision": d.precision, "count": d.live_count(),
                "bytes": d.bytes_used()}
            for t, d in self.tiers.items()
        }

    def save(self, out_dir: str | Path) -> None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        meta = {}
        for tier, d in self.tiers.items():
            d.index.save_index(str(out / f"index_{tier}.bin"))
            (out / f"ids_{tier}.json").write_text(
                json.dumps(d.ids, ensure_ascii=False), encoding="utf-8")
            np.save(out / f"vectors_{tier}.npy", d.vectors.astype(np.float16))
            meta[tier] = {"precision": d.precision, "capacity": d._capacity,
                          "deleted": sorted(d._deleted)}
        (out / "store_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                             encoding="utf-8")

    @classmethod
    def load(cls, in_dir: str | Path, dim: int, M: int, ef_construction: int, ef: int) -> "TierStore":
        src = Path(in_dir)
        meta = json.loads((src / "store_meta.json").read_text(encoding="utf-8"))
        store = cls(dim, M, ef_construction, ef)
        for tier, m in meta.items():
            store.init_tier(tier, m["precision"])
            d = store.tiers[tier]
            d.ids = json.loads((src / f"ids_{tier}.json").read_text(encoding="utf-8"))
            d.vectors = np.load(src / f"vectors_{tier}.npy").astype(np.float32)
            d._capacity = max(m["capacity"], len(d.ids))
            d.index.load_index(str(src / f"index_{tier}.bin"), max_elements=d._capacity)
            d.index.set_ef(ef)
            d._deleted.update(m.get("deleted", []))
            for r, nid in enumerate(d.ids):
                d.rows.setdefault(nid, []).append(r)
        return store
