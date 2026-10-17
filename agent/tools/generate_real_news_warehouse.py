#!/usr/bin/env python3
"""Export a new independent v8 real-content warehouse; no live Agent connection."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from simulation.warehouse_simulation.real_news import RealNewsSimulationConfig, export_dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New output directory; existing data is never overwritten")
    parser.add_argument("--start-at", default="2026-09-30T00:00:00+08:00")
    parser.add_argument("--closed-hours", type=int, default=356)
    parser.add_argument("--seed", type=int, default=20261014)
    args = parser.parse_args()
    try:
        config = RealNewsSimulationConfig(args.start_at, args.closed_hours, args.seed)
        manifest = export_dataset(config, args.output)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Generation failed: {exc}\n")
    print(json.dumps({"output": str(args.output.absolute()), "dataset_sha256": manifest["dataset_sha256"],
                      "real_article_count": manifest["real_article_count"], "news_content_synthetic": False,
                      "metric_data_synthetic": True,
                      "rows": {name: meta["rows"] for name, meta in manifest["tables"].items()}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
