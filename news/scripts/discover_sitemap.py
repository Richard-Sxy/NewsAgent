import argparse
import json
from datetime import date
from pathlib import Path

from crawler.tencent_sitemap import TencentSitemapCrawler
from config import settings
from service.ingest_repository import IngestRepository


def main() -> None:
    parser = argparse.ArgumentParser(
        description="从腾讯新闻 sitemap 发现指定日期的全量文章 URL",
    )
    parser.add_argument("--date", dest="target_date", required=True)
    parser.add_argument("--output", required=True, help="URL 列表输出文件")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--mark-discovered",
        action="store_true",
        help="将 URL 预先写入本地去重库，供可续跑批量入库使用",
    )
    args = parser.parse_args()

    try:
        date.fromisoformat(args.target_date)
    except ValueError:
        parser.error("--date 必须使用 YYYY-MM-DD 格式")

    result = TencentSitemapCrawler(workers=args.workers).discover(
        target_date=args.target_date,
        limit=args.limit,
    )
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(result.urls) + "\n", encoding="utf-8")

    if args.mark_discovered:
        repository = IngestRepository(settings.ingest_db_path)
        repository.mark_discovered_many([
            {
                "url": url,
                "source_title": "",
                "topic": "全量新闻",
                "publish_time": args.target_date,
            }
            for url in result.urls
        ])

    print(json.dumps({
        "target_date": args.target_date,
        "sitemap_count": result.sitemap_count,
        "fetched_sitemaps": result.fetched_sitemaps,
        "failed_sitemaps": result.failed_sitemaps,
        "discovered": len(result.urls),
        "output": str(output_path),
        "marked_discovered": args.mark_discovered,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
