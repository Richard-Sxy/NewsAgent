"""FAISS 三层存储（落地版）：单一事实源 + 三层紧凑索引 + 真实落盘 + 懒加载。

存储布局（out_dir/）：
    canonical_vectors.npy   fp32 事实源（mmap 读取，不常驻内存）
    canonical_ids.json      行号 -> news_id
    index_<tier>.faiss      hot=HNSW-Flat(fp32) / warm=HNSW-SQ8(int8) / cold=BHNSW(1bit)
    tier_pos_<tier>.json    该层索引位置 -> canonical 行号
    deleted_<tier>.json     逻辑删除的层内位置
    store_meta.json

  - HNSW 不支持 remove_ids，故删除用「位置 + 逻辑删除」，rebuild() 压实；
  - warm/cold 默认懒加载：首次检索才把索引读入内存。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import faiss
import numpy as np

from .quantize import BINARY, FP16, FP32, INT8

PRECISION_TO_SPEC = {FP32: "Flat", FP16: "SQ8", INT8: "SQ8"}


def to_binary(vectors: np.ndarray) -> np.ndarray:
    return np.packbits(vectors > 0, axis=1, bitorder="little").astype("uint8")


class _FaissTier:
    def __init__(self, tier, precision, M, ef_construction, ef):
        self.tier = tier
        self.precision = precision
        self.M = M
        self.ef_construction = ef_construction
        self.ef = ef
        self.positions: list[int] = []
        self.deleted: set[int] = set()
        self.index = None
        self.loaded = False

    def _new_index(self, dim):
        if self.precision == BINARY:
            return faiss.index_binary_factory(dim, f"BHNSW{self.M}")
        spec = f"HNSW{self.M},{PRECISION_TO_SPEC.get(self.precision, 'Flat')}"
        index = faiss.index_factory(dim, spec, faiss.METRIC_INNER_PRODUCT)
        index.hnsw.efConstruction = self.ef_construction
        index.hnsw.efSearch = self.ef
        return index

    def live_count(self) -> int:
        return len(self.positions) - len(self.deleted)


class FaissTierStore:
    def __init__(self, dim: int, M: int = 32, ef_construction: int = 200, ef: int = 64,
                 lazy_tiers: tuple[str, ...] = ("warm", "cold")):
        self.dim = dim
        self.M = M
        self.ef_construction = ef_construction
        self.ef = ef
        self.lazy_tiers = lazy_tiers
        self.tiers: dict[str, _FaissTier] = {}
        self.base_dir: Path | None = None
        self.canon_ids: list[str] = []
        self.canon_vecs = np.zeros((0, dim), dtype=np.float32)
        self.canon_loaded = False

    def init_tier(self, tier: str, precision: str) -> None:
        self.tiers[tier] = _FaissTier(tier, precision, self.M, self.ef_construction, self.ef)

    def _ensure_canonical(self) -> None:
        if self.canon_loaded:
            return
        if self.base_dir and (self.base_dir / "canonical_vectors.npy").exists():
            self.canon_vecs = np.load(self.base_dir / "canonical_vectors.npy", mmap_mode="r")
            self.canon_ids = json.loads(
                (self.base_dir / "canonical_ids.json").read_text(encoding="utf-8"))
        self.canon_loaded = True

    def _add_rows(self, tier: str, vectors: np.ndarray, canon_rows: list[int]) -> None:
        t = self.tiers[tier]
        if t.index is None:
            t.index = t._new_index(self.dim)
        vectors = np.ascontiguousarray(vectors, dtype=np.float32)
        if t.precision == BINARY:
            t.index.add(to_binary(vectors))
        else:
            if not t.index.is_trained:
                t.index.train(vectors)
            t.index.add(vectors)
        t.positions.extend(canon_rows)

    def add(self, tier: str, ids: list[str], vectors: np.ndarray) -> int:
        self._ensure_loaded(tier)
        self._ensure_canonical()
        vectors = np.ascontiguousarray(vectors, dtype=np.float32)
        base = len(self.canon_ids)
        self.canon_ids.extend(ids)
        self.canon_vecs = np.vstack([self.canon_vecs, vectors])
        self._add_rows(tier, vectors, list(range(base, base + len(ids))))
        return len(ids)

    def move(self, src: str, dst: str, ids: list[str]) -> int:
        self._ensure_loaded(src)
        self._ensure_loaded(dst)
        self._ensure_canonical()
        s = self.tiers[src]
        want = set(ids)
        rows, kept = [], []
        for pos, cr in enumerate(s.positions):
            if pos in s.deleted:
                continue
            if self.canon_ids[cr] in want:
                rows.append(cr)
                kept.append(self.canon_ids[cr])
        if not rows:
            return 0
        vecs = np.asarray(self.canon_vecs[rows], dtype=np.float32)
        self._add_rows(dst, vecs, rows)
        for pos, cr in enumerate(s.positions):
            if pos not in s.deleted and self.canon_ids[cr] in want:
                s.deleted.add(pos)
        return len(rows)

    def _ensure_loaded(self, tier: str) -> None:
        t = self.tiers[tier]
        if t.loaded:
            return
        path = self.base_dir / f"index_{tier}.faiss" if self.base_dir else None
        if path and path.exists():
            t.index = (faiss.read_index_binary(str(path)) if t.precision == BINARY
                       else faiss.read_index(str(path)))
            if t.precision != BINARY:
                t.index.hnsw.efSearch = self.ef
            t.positions = json.loads(
                (self.base_dir / f"tier_pos_{tier}.json").read_text(encoding="utf-8"))
            dpath = self.base_dir / f"deleted_{tier}.json"
            t.deleted = set(json.loads(dpath.read_text(encoding="utf-8"))) if dpath.exists() else set()
        else:
            self._ensure_canonical()
            if t.index is None:
                t.index = t._new_index(self.dim)
        t.loaded = True

    def search(self, tier: str, query: np.ndarray, k: int):
        self._ensure_loaded(tier)
        t = self.tiers[tier]
        if t.index is None or not t.positions:
            return [], []
        extra = k + len(t.deleted)
        q = np.asarray(query, dtype=np.float32)
        if q.ndim == 1:
            q = q[None, :]
        q = to_binary(q) if t.precision == BINARY else np.ascontiguousarray(q, dtype=np.float32)
        D, I = t.index.search(q, extra)
        out_ids, out_d = [], []
        for dist, pos in zip(D[0], I[0]):
            if pos < 0 or pos in t.deleted or pos >= len(t.positions):
                continue
            self._ensure_canonical()
            out_ids.append(self.canon_ids[t.positions[pos]])
            out_d.append(float(dist))
            if len(out_ids) >= k:
                break
        return out_ids, out_d

    def delete(self, tier: str, ids: list[str]) -> int:
        self._ensure_loaded(tier)
        t = self.tiers[tier]
        want = set(ids)
        n = 0
        for pos, canon_row in enumerate(t.positions):
            if pos in t.deleted:
                continue
            if self.canon_ids[canon_row] in want:
                t.deleted.add(pos)
                n += 1
        return n

    def vectors_of(self, tier: str, ids: list[str]) -> tuple[np.ndarray, list[str]]:
        self._ensure_loaded(tier)
        self._ensure_canonical()
        t = self.tiers[tier]
        want = set(ids)
        rows, kept = [], []
        for pos, canon_row in enumerate(t.positions):
            if pos in t.deleted:
                continue
            nid = self.canon_ids[canon_row]
            if nid in want:
                rows.append(canon_row)
                kept.append(nid)
        if not rows:
            return np.zeros((0, self.dim), dtype=np.float32), []
        return np.asarray(self.canon_vecs[rows], dtype=np.float32), kept

    def rebuild(self, tier: str) -> None:
        self._ensure_loaded(tier)
        self._ensure_canonical()
        t = self.tiers[tier]
        live = [p for p in range(len(t.positions)) if p not in t.deleted]
        rows = [t.positions[p] for p in live]
        vecs = np.asarray(self.canon_vecs[rows], dtype=np.float32) if rows else np.zeros((0, self.dim), np.float32)
        t.index = t._new_index(self.dim)
        t.positions = []
        t.deleted = set()
        if rows:
            if t.precision == BINARY:
                t.index.add(to_binary(vecs))
            else:
                if not t.index.is_trained:
                    t.index.train(vecs)
                t.index.add(vecs)
            t.positions = list(rows)

    def stats(self) -> dict:
        out = {}
        canon = 0
        if self.base_dir:
            cp = self.base_dir / "canonical_vectors.npy"
            canon = cp.stat().st_size if cp.exists() else 0
        for tier, t in self.tiers.items():
            ib = 0
            if self.base_dir:
                ip = self.base_dir / f"index_{tier}.faiss"
                ib = ip.stat().st_size if ip.exists() else 0
            out[tier] = {"precision": t.precision, "count": t.live_count(),
                         "index_bytes": ib, "index_mb": round(ib / 1048576, 2),
                         "loaded": t.loaded}
        out["canonical_bytes"] = canon
        out["canonical_mb"] = round(canon / 1048576, 2)
        return out

    def save(self, out_dir: str | Path) -> None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        self.base_dir = out
        tmp = out / "canonical_vectors.npy.tmp"
        np.save(tmp, np.array(self.canon_vecs, dtype=np.float32))
        os.replace(tmp, out / "canonical_vectors.npy")
        (out / "canonical_ids.json").write_text(
            json.dumps(self.canon_ids, ensure_ascii=False), encoding="utf-8")
        meta = {}
        for tier, t in self.tiers.items():
            self.rebuild(tier)
            self._ensure_loaded(tier)
            path = out / f"index_{tier}.faiss"
            if t.precision == BINARY:
                faiss.write_index_binary(t.index, str(path))
            else:
                faiss.write_index(t.index, str(path))
            (out / f"tier_pos_{tier}.json").write_text(
                json.dumps(t.positions), encoding="utf-8")
            (out / f"deleted_{tier}.json").write_text(
                json.dumps(sorted(t.deleted)), encoding="utf-8")
            meta[tier] = {"precision": t.precision, "count": len(t.positions)}
        (out / "store_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                             encoding="utf-8")

    @classmethod
    def load(cls, in_dir: str | Path, dim: int, M: int, ef_construction: int, ef: int,
             lazy_tiers: tuple[str, ...] = ("warm", "cold")) -> "FaissTierStore":
        src = Path(in_dir)
        meta = json.loads((src / "store_meta.json").read_text(encoding="utf-8"))
        store = cls(dim, M, ef_construction, ef, lazy_tiers=lazy_tiers)
        store.base_dir = src
        store._ensure_canonical()
        for tier, m in meta.items():
            store.init_tier(tier, m["precision"])
            if tier not in lazy_tiers:
                store._ensure_loaded(tier)
        return store
