import argparse
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from config import settings
from service.discovery_ingest_service import DiscoveryIngestService


DEFAULT_SOURCES = {
    "科技": "https://news.qq.com/ch/tech/",
    "体育": "https://news.qq.com/ch/sports/",
    "财经": "https://news.qq.com/ch/finance/",
    "娱乐": "https://news.qq.com/ch/ent/",
    "汽车": "https://news.qq.com/ch/auto/",
    "教育": "https://news.qq.com/ch/edu/",
}


def get_today(timezone_name: str) -> str:
    """获取指定时区的当天日期。"""
    timezone = ZoneInfo(timezone_name)
    return datetime.now(timezone).date().isoformat()


def has_source_failure(result: dict) -> bool:
    """判断是否有分类发现失败。"""
    return any(
        source.get("status") == "failed"
        for source in result.get("sources", [])
    )


def has_ingest_failure(result: dict) -> bool:
    """判断是否有单篇新闻入库失败。"""
    ingest = result.get("ingest")
    if not ingest:
        return False
    return (
        ingest.get("failed", 0) > 0
        or ingest.get("retry_exhausted", 0) > 0
    )

# 这边主要是每日新闻获取的部分
def main() -> int:
    parser = argparse.ArgumentParser(
        description="每日发现腾讯分类新闻并导入 FastGPT",
    )
    parser.add_argument(
        "--date",
        dest="target_date",
        help="指定处理日期，格式 YYYY-MM-DD；默认使用配置时区的当天日期",
    )
    parser.add_argument(
        "--limit-per-topic",
        type=int,
        default=settings.daily_limit_per_topic,
        help="每个分类最多导入数量",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=settings.daily_max_pages,
        help="每个分类最多请求页数",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只发现新闻，不写数据库、不入库",
    )
    args = parser.parse_args()

    target_date = args.target_date or ""
    try:
        target_date = target_date or get_today(settings.daily_timezone)
        datetime.strptime(target_date, "%Y-%m-%d")
        result = DiscoveryIngestService().discover_and_ingest(
            sources=DEFAULT_SOURCES,
            limit_per_topic=args.limit_per_topic,
            target_date=target_date,
            max_pages=args.max_pages,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "target_date": target_date,
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1

    failed = has_source_failure(result) or has_ingest_failure(result)
    output = {
        "status": "partial_failed" if failed else "success",
        "target_date": target_date,
        **result,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
