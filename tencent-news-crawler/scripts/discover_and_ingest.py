import argparse
import json
from datetime import date

from service.discovery_ingest_service import DiscoveryIngestService

""""""
def parse_sources(values: list[str]) -> dict[str, str]:
    sources = {}
    for value in values:
        topic, separator, url = value.partition("=")
        topic = topic.strip()
        url = url.strip()
        if not separator or not topic or not url:
            raise ValueError(
                f"来源格式错误: {value!r}，应使用 分类=URL。"
            )
        if topic in sources:
            raise ValueError(f"分类重复: {topic}")
        sources[topic] = url
    return sources


def main() -> None:
    parser = argparse.ArgumentParser(
        description="发现腾讯分类新闻并导入 FastGPT",
    )
    parser.add_argument(
        "--source",
        action="append",
        required=True,
        help="分类来源，可重复传入，例如 科技=https://news.qq.com/ch/tech/",
    )
    parser.add_argument(
        "--limit-per-topic",
        type=int,
        default=10,
        help="每个分类最多发现的文章数，默认 10",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只显示发现结果，不抓取正文、不导入 FastGPT",
    )
    parser.add_argument(
        "--date",
        dest="target_date",
        help="只处理指定日期的新闻，格式 YYYY-MM-DD",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=3,
        help="每个分类最多请求的页数，默认 3",
    )
    args = parser.parse_args()

    if args.target_date:
        try:
            date.fromisoformat(args.target_date)
        except ValueError:
            parser.error("--date 必须使用 YYYY-MM-DD 格式")

    try:
        result = DiscoveryIngestService().discover_and_ingest(
            sources=parse_sources(args.source),
            limit_per_topic=args.limit_per_topic,
            target_date=args.target_date,
            max_pages=args.max_pages,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        parser.exit(1, f"自动发现或入库失败：{exc}\n")

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
