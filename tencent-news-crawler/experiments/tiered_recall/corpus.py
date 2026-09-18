"""语料读取与时间分区工具。"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

DATE_RE = re.compile(r"发布时间[:：]\s*(\d{4}-\d{2}-\d{2})")


def load_chunks(exp_dir: str | Path) -> list[dict]:
    path = Path(exp_dir) / "chunks.jsonl"
    return [json.loads(line) for line in path.open(encoding="utf-8")]


def publish_dates(chunks: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for c in chunks:
        nid = c["news_id"]
        if nid in out:
            continue
        pt = (c.get("publish_time") or "")[:10]
        if not pt:
            m = DATE_RE.search(c["text"])
            pt = m.group(1) if m else ""
        if pt:
            out[nid] = pt
    return out


def partition_of(publish: str) -> str:
    return publish[:7]


def news_meta(pubdates: dict[str, str]) -> dict[str, dict]:
    return {nid: {"publish": p, "partition": partition_of(p)} for nid, p in pubdates.items()}


def rank_tier_map(pubdates: dict[str, str], hot_parts: int, warm_parts: int) -> dict[str, str]:
    """按分区新旧排序切层：最新 hot_parts 个分区为 hot，其次 warm_parts 个为 warm，其余 cold。"""
    parts = sorted({partition_of(p) for p in pubdates.values()}, reverse=True)
    tiers: dict[str, str] = {}
    for i, part in enumerate(parts):
        tiers[part] = "hot" if i < hot_parts else ("warm" if i < hot_parts + warm_parts else "cold")
    return tiers


def max_publish(pubdates: dict[str, str]) -> date:
    return date.fromisoformat(max(pubdates.values()))
