"""从正文缓存构建可人工复核的事件级聚类数据集。"""

import argparse
from pathlib import Path

from config import settings
from intelligence.dataset import load_cached_articles
from intelligence.event_clustering import EventClusterer


def main() -> int:
    parser = argparse.ArgumentParser(description="构建新闻事件聚类基线数据集")
    parser.add_argument("--cache-dir", default=settings.article_cache_dir)
    parser.add_argument("--db-path", default=settings.ingest_db_path)
    parser.add_argument("--output", default="data/events/event_clusters.json")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--threshold", type=float, default=0.48)
    parser.add_argument("--max-time-gap-days", type=float, default=3.0)
    args = parser.parse_args()

    articles = load_cached_articles(args.cache_dir, args.db_path, args.limit)
    dataset = EventClusterer(
        threshold=args.threshold,
        max_time_gap_days=args.max_time_gap_days,
    ).cluster(articles)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        dataset.model_dump_json(indent=2),
        encoding="utf-8",
    )
    print(
        f"articles={dataset.article_count} clusters={dataset.cluster_count} "
        f"multi_article_clusters={dataset.multi_article_cluster_count} "
        f"output={output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
