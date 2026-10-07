"""执行不访问企业服务的热点故障分类与监控演练。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.evaluation.fault_drill import (
    FaultDrillRunner,
    load_fault_drill_dataset,
)


DEFAULT_PLAN = (
    Path(__file__).parent
    / "fault_scenarios"
    / "enterprise_rpc_faults_v1.json"
)


async def main_async() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    args = parser.parse_args()
    report = await FaultDrillRunner().run(
        load_fault_drill_dataset(args.plan)
    )
    print(report.model_dump_json(indent=2))
    if report.pass_rate != 1:
        raise SystemExit(1)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
