#!/usr/bin/env python3
"""Generate isolated v7 aggregate fixtures; no database or credentials required."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simulation.warehouse_simulation.tencent_news import SimulationConfig, export_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New directory; existing data is never overwritten")
    parser.add_argument("--news-per-tenant", type=int, default=120)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--start-date", default="2026-10-07")
    parser.add_argument("--seed", type=int, default=20261014)
    args = parser.parse_args()
    try:
        config = SimulationConfig(args.news_per_tenant, args.days, args.start_date, args.seed)
        manifest = export_dataset(config, args.output)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Generation failed: {exc}\n")
    print(json.dumps({"output": str(args.output.absolute()), "dataset_sha256": manifest["dataset_sha256"],
                      "synthetic": True, "rows": {name: value["rows"] for name, value in manifest["tables"].items()}},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
