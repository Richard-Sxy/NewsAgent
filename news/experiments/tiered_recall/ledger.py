"""TierLedger：分区层归属、个体层覆盖与晋升计数台账（JSON，原子写）。"""

from __future__ import annotations

import json
import os
from pathlib import Path


class TierLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict = {"partitions": {}, "vectors": {}, "streak": {}, "history": []}
        if self.path.exists():
            self.load()

    def load(self) -> None:
        self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def get_partition_tier(self, partition: str) -> str | None:
        return self.data["partitions"].get(partition)

    def set_partition_tier(self, partition: str, tier: str) -> None:
        self.data["partitions"][partition] = tier

    def get_vector_tier(self, news_id: str) -> str | None:
        return self.data["vectors"].get(news_id)

    def set_vector_tier(self, news_id: str, tier: str) -> None:
        self.data["vectors"][news_id] = tier

    def get_streak(self, news_id: str) -> int:
        return int(self.data["streak"].get(news_id, 0))

    def set_streak(self, news_id: str, streak: int) -> None:
        self.data["streak"][news_id] = streak

    def record(self, move: dict) -> None:
        self.data["history"].append(move)
