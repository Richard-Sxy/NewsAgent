#!/usr/bin/env python3
"""Freeze existing verified public-news content for the independent v8 warehouse."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simulation.warehouse_simulation.real_news_catalog import CATALOG_PATH, freeze_catalog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=CATALOG_PATH.parent,
                        help="New directory; existing output is never overwritten")
    args = parser.parse_args(argv)
    try:
        report = freeze_catalog(args.output_dir)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Catalog freeze failed: {exc}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
