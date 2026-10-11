"""冷/温/热三层召回实验骨架。

两种存储后端：
  - hnswlib（内存模拟，快速验证逻辑）
  - faiss（真落盘、真 int8/binary 索引、懒加载）
"""

from __future__ import annotations


def make_store(backend: str, dim: int, **kw):
    if backend == "faiss":
        from .faiss_store import FaissTierStore
        return FaissTierStore(dim, **kw)
    from .store import TierStore
    return TierStore(dim, **kw)


def load_store(backend: str, path, dim: int, M: int, ef_construction: int, ef: int):
    if backend == "faiss":
        from .faiss_store import FaissTierStore
        return FaissTierStore.load(path, dim, M, ef_construction, ef)
    from .store import TierStore
    return TierStore.load(path, dim, M, ef_construction, ef)
