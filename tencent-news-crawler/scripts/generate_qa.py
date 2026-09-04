import argparse
import json
import sys

from config import settings
from service.news_qa_service import NewsQAService


def main() -> int:
    parser = argparse.ArgumentParser(
        description="将已入库腾讯新闻提交到 FastGPT 生成 QA",
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="可选：只处理指定腾讯新闻 URL",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=settings.daily_qa_limit,
        help=(
            "未指定 URL 时处理的新闻数量；"
            "默认读取 DAILY_QA_LIMIT"
        ),
    )
    args = parser.parse_args()

    try:
        service = NewsQAService()
        if args.url:
            result = service.generate_for_url(args.url)
        else:
            result = service.generate_latest(args.limit)
    except Exception as exc:
        print(f"QA 生成提交失败：{exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if isinstance(result, dict) and result.get("failed", 0) > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
