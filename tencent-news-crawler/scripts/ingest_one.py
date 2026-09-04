import argparse
import json

from service.news_ingest_service import NewsIngestService


def main() -> None:
    """抓取并导入一篇腾讯新闻，输出本地入库结果。"""
    parser = argparse.ArgumentParser(
        description="抓取一篇腾讯新闻并导入 FastGPT",
    )
    parser.add_argument(
        "url",
        help="腾讯新闻正文 URL",
    )
    args = parser.parse_args()

    try:
        result = NewsIngestService().ingest_url(args.url)
    except Exception as exc:
        parser.exit(
            status=1,
            message=f"入库失败: {exc}\n",
        )

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
