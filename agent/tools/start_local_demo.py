#!/usr/bin/env python3
"""Run the complete local simulation, with no enterprise model configuration."""

from __future__ import annotations

from pathlib import Path
import sys


# Support direct execution from any working directory and imports in tests.
TOOLS_DIR = str(Path(__file__).resolve().parent)
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from deploy_bge_demo import argument_parser, main as deployment_main  # noqa: E402


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(value == "--model" or value.startswith("--model=") for value in arguments):
        print("此入口固定使用本地模拟模型。企业模式请使用 deploy_bge_demo.py。", file=sys.stderr)
        return 2
    if "--help" in arguments or "-h" in arguments:
        argument_parser(local_only=True).print_help()
        return 0
    return deployment_main([*arguments, "--model", "local"])


if __name__ == "__main__":
    raise SystemExit(main())
