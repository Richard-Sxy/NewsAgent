"""Local read-only label search; does not expose individual synthetic profiles."""

import argparse
from datetime import datetime
import json

from app.behavior_tags.runtime import LocalBehaviorRuntime


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--window-start", required=True, type=datetime.fromisoformat)
    parser.add_argument("--window-end", required=True, type=datetime.fromisoformat)
    parser.add_argument("--include", action="append", default=[])
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--behavior", action="append", default=[])
    parser.add_argument("--match", choices=["any", "all"], default="any")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--dataset-version")
    parser.add_argument("--scenario-id")
    args = parser.parse_args()
    runtime = LocalBehaviorRuntime(args.package, environment="local")
    rows = runtime.search(tenant_id=args.tenant_id, window_start=args.window_start,
        window_end=args.window_end, include_topic_ids=tuple(args.include),
        exclude_topic_ids=tuple(args.exclude), behavior_label_ids=tuple(args.behavior),
        match=args.match, limit=args.limit, dataset_version=args.dataset_version, scenario_id=args.scenario_id)
    print(json.dumps({"source_mode": "synthetic", "package_sha256": runtime.package_sha256,
        "results": rows}, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
