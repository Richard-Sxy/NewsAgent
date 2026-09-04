import argparse
import json
from pathlib import Path

from service.news_ingest_service import (
    NewsIngestService,
)

"""加载路径"""
def load_urls(file_path: str) -> list[str]:
    path = Path(file_path)

    if not path.is_file():
        raise ValueError(
            f"URL 文件不存在: {file_path}"
        )

    urls = []

    for line in path.read_text(
        encoding="utf-8"
    ).splitlines():
        line = line.strip()

        if not line:
            continue

        if line.startswith("#"):
            continue

        urls.append(line)

    return urls

def main() -> None:
    parser = argparse.ArgumentParser(
        description="批量抓取腾讯新闻并导入到FastGPT",
    )
    parser.add_argument(
        "file",
        help="URL 文本文件, 每一行一条 URL",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=20,
        help="允许导入的最大 URL 数量",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="并发入库线程数，默认 1",
    )

    args = parser.parse_args()

    try:
        urls = load_urls(args.file)

        result = NewsIngestService().ingest_urls(
            urls=urls,
            max_items=args.max_items,
            workers=args.workers,
        )

    except Exception as exc:
        parser.exit(
            status=1,
            message=f"批量入库失败：{exc}\n"
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
