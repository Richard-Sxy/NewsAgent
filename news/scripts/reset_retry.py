import argparse
import json

from config import settings
from service.ingest_repository import IngestRepository
from service.qa_repository import QARepository


def main() -> int:
    parser = argparse.ArgumentParser(
        description="人工重置新闻原文或QA的失败重试计数",
    )
    parser.add_argument("url", help="腾讯新闻URL")
    parser.add_argument(
        "--target",
        choices=("qa", "ingest"),
        default="qa",
        help="重置QA或原文入库记录，默认qa",
    )
    args = parser.parse_args()

    repository = (
        QARepository(settings.ingest_db_path)
        if args.target == "qa"
        else IngestRepository(settings.ingest_db_path)
    )
    record = repository.get_by_url(args.url)
    if not record:
        print(json.dumps({
            "status": "not_found",
            "target": args.target,
            "url": args.url,
        }, ensure_ascii=False, indent=2))
        return 1

    repository.reset_retry(args.url)
    updated = repository.get_by_url(args.url)
    print(json.dumps({
        "status": "reset",
        "target": args.target,
        "url": args.url,
        "retry_count": updated["retry_count"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
