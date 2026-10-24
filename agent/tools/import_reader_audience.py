#!/usr/bin/env python3
"""在显式本地 PostgreSQL 数据库中导入/查询冻结的匿名读取器聚合。"""
from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.modules.reader_profiles.warehouse import bind_unified_content, import_package, query_audience


def encode(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    raise TypeError(type(value).__name__)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="optional JSON result file")
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import")
    importer.add_argument("--package", required=True)
    binder = commands.add_parser("bind-content")
    binder.add_argument("--release", required=True)
    binder.add_argument("--content-tenant", required=True)
    binder.add_argument("--content-release", required=True)
    query = commands.add_parser("query")
    for name in ("release", "tenant", "start", "end"):
        query.add_argument("--" + name, required=True)
    query.add_argument("--news-id", action="append", required=True)
    query.add_argument("--lookback-hours", type=int, choices=(24, 720), default=720)
    query.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    dsn = os.environ.get("AUDIENCE_DATABASE_URL")
    if not dsn:
        parser.error("set AUDIENCE_DATABASE_URL explicitly; .env is never loaded")
    if dsn.startswith("postgresql+psycopg://"):
        dsn = "postgresql://" + dsn.split("://", 1)[1]
    if args.command == "import":
        result = import_package(dsn, args.package, progress=lambda message: print(message, file=sys.stderr, flush=True))
    elif args.command == "bind-content":
        result = bind_unified_content(dsn, args.release, args.content_tenant, args.content_release)
    else:
        result = query_audience(dsn, release=args.release, tenant=args.tenant, start=args.start,
            end=args.end, news_ids=args.news_id, lookback_hours=args.lookback_hours, top_k=args.top_k)
    payload = json.dumps(result, ensure_ascii=False, indent=2, default=encode) + "\n"
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload)
    print(payload, end="")


if __name__ == "__main__":
    main()
