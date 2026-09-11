"""热点监控 Schedule 的运维 CLI。

用法（在 writing-agent-service 目录下）：

    python -m app.hot_news_scheduler ensure     # 按配置幂等创建/更新全部 Schedule
    python -m app.hot_news_scheduler list       # 列出配置定义的租户组与 Schedule ID
    python -m app.hot_news_scheduler pause <tenant-group>
    python -m app.hot_news_scheduler unpause <tenant-group>
    python -m app.hot_news_scheduler delete <tenant-group>

Schedule 定义只来自环境变量 HOT_NEWS_SCHEDULE_DEFINITIONS_JSON
（每个租户组一个 Schedule）；暂停/恢复/删除是显式运维动作。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from temporalio.client import Client

from app.config import get_settings
from app.services.hot_news_schedule import (
    HotNewsScheduleConfigError,
    ensure_schedules,
    parse_schedule_definitions,
    schedule_id_for,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.hot_news_scheduler",
        description="管理热点监控 Temporal Schedule（每个租户组一个）",
    )
    parser.add_argument(
        "command",
        choices=["ensure", "list", "pause", "unpause", "delete"],
    )
    parser.add_argument("tenant_group", nargs="?")
    return parser


async def cmd_ensure() -> int:
    settings = get_settings()
    definitions = parse_schedule_definitions(
        settings.hot_news_schedule_definitions_json
    )
    if not definitions:
        print("HOT_NEWS_SCHEDULE_DEFINITIONS_JSON 为空，无需 provisioning")
        return 0
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
    )
    results = await ensure_schedules(
        client,
        definitions,
        task_queue=settings.temporal_hot_news_task_queue,
    )
    for result in results:
        print(
            f"{result.action}: {result.schedule_id} "
            f"(tenant_group={result.tenant_group})"
        )
    return 0


def cmd_list() -> int:
    settings = get_settings()
    definitions = parse_schedule_definitions(
        settings.hot_news_schedule_definitions_json
    )
    if not definitions:
        print("未配置任何热点 Schedule 定义")
        return 0
    for definition in definitions:
        state = "paused" if definition.paused else "active"
        print(
            f"{schedule_id_for(definition.tenant_group)} "
            f"tenant={definition.tenant_id} "
            f"bundle={definition.production_bundle_version} "
            f"window={definition.window_minutes}m "
            f"every={definition.interval_minutes}m [{state}]"
        )
    return 0


async def cmd_state(command: str, tenant_group: str) -> int:
    settings = get_settings()
    schedule_id = schedule_id_for(tenant_group)
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
    )
    handle = client.get_schedule_handle(schedule_id)
    if command == "pause":
        await handle.pause(note=f"paused by operator for {tenant_group}")
    elif command == "unpause":
        await handle.unpause(note=f"resumed by operator for {tenant_group}")
    else:
        await handle.delete()
    print(f"{command}: {schedule_id}")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "ensure":
            return asyncio.run(cmd_ensure())
        if args.command == "list":
            return cmd_list()
        if not args.tenant_group:
            print(f"{args.command} 需要 tenant_group 参数", file=sys.stderr)
            return 2
        return asyncio.run(cmd_state(args.command, args.tenant_group))
    except HotNewsScheduleConfigError as exc:
        print(f"调度配置非法：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
