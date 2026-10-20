#!/usr/bin/env python3
"""Freeze real v2 public-news metadata/features for the v8 warehouse extension."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simulation.warehouse_simulation.real_news_catalog import EXTENSION_PATH, freeze_extension


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=EXTENSION_PATH)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = freeze_extension(args.output, overwrite=args.overwrite)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Extension freeze failed: {exc}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
