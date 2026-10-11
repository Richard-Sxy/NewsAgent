"""CLI: python -m experiments.tiered_recall {build,simulate,eval} ..."""

from __future__ import annotations

import sys

USAGE = "usage: python -m experiments.tiered_recall {build,simulate,eval} [options]"


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("build", "simulate", "eval"):
        print(USAGE)
        raise SystemExit(2)
    cmd = sys.argv[1]
    sys.argv = [sys.argv[0], *sys.argv[2:]]
    if cmd == "build":
        from .build_tiers import main as run
    elif cmd == "simulate":
        from .simulate import main as run
    else:
        from .evaluate import main as run
    run()


if __name__ == "__main__":
    main()
