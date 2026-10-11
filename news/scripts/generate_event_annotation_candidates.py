"""生成供人工标注的新闻事件文章对。"""

import argparse
import json
import random
from datetime import datetime
from pathlib import Path

from config import settings
from intelligence.dataset import load_cached_articles
from intelligence.event_clustering import TfidfSpace, temporal_similarity

""" 解析时间 """
def parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None

""" 构建候选集 """
def build_candidates(articles, limit: int, seed: int = 42) -> list[dict]:
    title_space = TfidfSpace([article.title for article in articles])
    content_space = TfidfSpace([
        f"{article.title} {article.content[:800]}" for article in articles
    ])
    scored = []
    for left_index, left in enumerate(articles):
        left_time = parse_time(left.publish_time)
        for right_index in range(left_index + 1, len(articles)):
            right = articles[right_index]
            right_time = parse_time(right.publish_time)
            if left_time and right_time:
                gap = abs((left_time - right_time).total_seconds()) / 86400
                if gap > 3:
                    continue
            title_score = TfidfSpace.cosine(
                title_space.vectors[left_index], title_space.vectors[right_index]
            )
            content_score = TfidfSpace.cosine(
                content_space.vectors[left_index], content_space.vectors[right_index]
            )
            score = (
                0.65 * title_score
                + 0.25 * content_score
                + 0.10 * temporal_similarity(left, right, 1.5)
            )
            scored.append((score, left_index, right_index))

    scored.sort(reverse=True)
    high_count = int(limit * 0.5)
    medium_count = int(limit * 0.35)
    selected = [("high_similarity", *row) for row in scored[:high_count]]

    medium = [row for row in scored if 0.20 <= row[0] < 0.48]
    selected.extend(("hard_negative", *row) for row in medium[:medium_count])

    selected_keys = {(left, right) for _, _, left, right in selected}
    remaining = [row for row in scored if (row[1], row[2]) not in selected_keys]
    random.Random(seed).shuffle(remaining)
    selected.extend(
        ("random_negative", *row)
        for row in remaining[: max(0, limit - len(selected))]
    )

    candidates = []
    for number, (candidate_type, score, left_index, right_index) in enumerate(
        selected[:limit], start=1
    ):
        left = articles[left_index]
        right = articles[right_index]
        candidates.append({
            "pair_id": f"pair-{number:04d}",
            "left_article_id": left.article_id,
            "left_title": left.title,
            "left_publish_time": left.publish_time,
            "right_article_id": right.article_id,
            "right_title": right.title,
            "right_publish_time": right.publish_time,
            "baseline_score": round(score, 4),
            "candidate_type": candidate_type,
            "relation": "",
            "note": "",
        })
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(description="生成事件关系人工标注候选")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument(
        "--output",
        default="datasets/annotations/event_pairs_candidates.jsonl",
    )
    parser.add_argument("--cache-dir", default=settings.article_cache_dir)
    parser.add_argument("--db-path", default=settings.ingest_db_path)
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit 必须大于 0")

    articles = load_cached_articles(args.cache_dir, args.db_path)
    candidates = build_candidates(articles, args.limit)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in candidates) + "\n",
        encoding="utf-8",
    )

    markdown = output.with_suffix(".md")
    lines = [
        "# 事件关系人工标注候选",
        "",
        "在 JSONL 文件中将 relation 填为 same_event、related_event 或 unrelated。",
        "",
    ]
    for row in candidates:
        lines.extend([
            f"## {row['pair_id']} · score={row['baseline_score']}",
            "",
            f"- A：{row['left_title']}（{row['left_publish_time']}）",
            f"- B：{row['right_title']}（{row['right_publish_time']}）",
            "- relation：",
            "",
        ])
    markdown.write_text("\n".join(lines), encoding="utf-8")
    print(f"candidates={len(candidates)} output={output} preview={markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
