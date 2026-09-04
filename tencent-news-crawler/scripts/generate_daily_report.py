import argparse
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from config import settings
from service.daily_report_service import DailyReportService


def main() -> int:
    parser = argparse.ArgumentParser(description="生成新闻和 QA 每日统计报告")
    parser.add_argument("--date", dest="target_date", help="报告日期 YYYY-MM-DD")
    parser.add_argument("--output-dir", default=settings.daily_report_dir)
    parser.add_argument("--ingest-exit-code", type=int)
    parser.add_argument("--qa-exit-code", type=int)
    parser.add_argument("--evaluation-exit-code", type=int)
    args = parser.parse_args()

    target_date = args.target_date or datetime.now(
        ZoneInfo(settings.daily_timezone)
    ).date().isoformat()
    try:
        service = DailyReportService(
            db_path=settings.ingest_db_path,
            article_cache_dir=settings.article_cache_dir,
            timezone_name=settings.daily_timezone,
            ingest_max_retry_count=settings.ingest_max_retry_count,
            qa_max_retry_count=settings.qa_max_retry_count,
        )
        report = service.build(
            target_date,
            ingest_exit_code=args.ingest_exit_code,
            qa_exit_code=args.qa_exit_code,
            evaluation_exit_code=args.evaluation_exit_code,
        )
        paths = service.write(report, args.output_dir)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    print(json.dumps({**report, "files": paths}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
