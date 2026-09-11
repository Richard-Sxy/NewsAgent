"""热点监控 Temporal Schedule 的声明式管理。

设计决策：每个租户组一个 Schedule，Schedule ID 由租户组稳定推导
（``hot-news-monitor-{tenant_group}``），因此 provisioning 是幂等的：
不存在则创建，已存在则按最新定义整体更新 spec。Schedule 触发的是
``HotNewsWindowDispatcherWorkflow``，由它在每次触发时计算
``[now - window_minutes, now)`` 窗口并启动热点监控子 Workflow。

Schedule 定义只能来自部署配置（``HOT_NEWS_SCHEDULE_DEFINITIONS_JSON``），
不接受 API 或 Agent 写入；暂停/恢复/删除属于运维动作，通过 CLI 执行。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
    ScheduleUpdateInput,
)

from app.workflows.contracts import HotNewsWindowDispatchRequest
from app.workflows.hot_news import HOT_NEWS_DISPATCH_WORKFLOW_NAME

TENANT_GROUP_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")
SCHEDULE_ID_PREFIX = "hot-news-monitor"


class HotNewsScheduleDefinition(BaseModel):
    """一个租户组的热点周期调度定义。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_group: str = Field(min_length=2, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=128)
    production_bundle_version: str = Field(min_length=1, max_length=128)
    window_minutes: int = Field(ge=5, le=1440)
    interval_minutes: int = Field(ge=5, le=1440)
    paused: bool = False

    def model_post_init(self, __context: Any, /) -> None:
        if not TENANT_GROUP_PATTERN.match(self.tenant_group):
            raise ValueError(
                "tenant_group must be a lowercase slug "
                "(letters, digits, hyphens; 2-64 chars)"
            )


class HotNewsScheduleConfigError(ValueError):
    """调度定义配置非法（JSON、字段或重复租户组）。"""


@dataclass(frozen=True, slots=True)
class HotNewsScheduleEnsureResult:
    schedule_id: str
    tenant_group: str
    action: Literal["created", "updated"]


class _ScheduleHandle(Protocol):
    async def update(
        self,
        updater: Any,
    ) -> None: ...


class _ScheduleClient(Protocol):
    """测试可用伪实现的最小客户端协议。"""

    async def create_schedule(self, id: str, schedule: Schedule) -> Any: ...

    def get_schedule_handle(self, id: str) -> _ScheduleHandle: ...


def schedule_id_for(tenant_group: str) -> str:
    """从租户组稳定推导 Schedule ID；同组重复 provisioning 命中同一 ID。"""

    if not TENANT_GROUP_PATTERN.match(tenant_group):
        raise HotNewsScheduleConfigError(
            f"tenant_group 不是合法的 slug：{tenant_group!r}"
        )
    return f"{SCHEDULE_ID_PREFIX}-{tenant_group}"


def parse_schedule_definitions(
    raw_json: str,
) -> tuple[HotNewsScheduleDefinition, ...]:
    """解析并校验部署配置中的调度定义数组。"""

    try:
        raw = json.loads(raw_json or "[]")
    except json.JSONDecodeError as exc:
        raise HotNewsScheduleConfigError(
            "HOT_NEWS_SCHEDULE_DEFINITIONS_JSON 不是合法 JSON"
        ) from exc
    if not isinstance(raw, list):
        raise HotNewsScheduleConfigError(
            "HOT_NEWS_SCHEDULE_DEFINITIONS_JSON 必须是 JSON 数组"
        )

    definitions: list[HotNewsScheduleDefinition] = []
    seen_groups: set[str] = set()
    for index, item in enumerate(raw):
        try:
            definition = HotNewsScheduleDefinition.model_validate(item)
        except (ValidationError, ValueError) as exc:
            raise HotNewsScheduleConfigError(
                f"第 {index} 条热点调度定义非法：{exc}"
            ) from exc
        if definition.tenant_group in seen_groups:
            raise HotNewsScheduleConfigError(
                f"重复的 tenant_group：{definition.tenant_group!r}；"
                "每个租户组只能有一个热点 Schedule"
            )
        seen_groups.add(definition.tenant_group)
        definitions.append(definition)
    return tuple(definitions)


def build_schedule(
    definition: HotNewsScheduleDefinition,
    *,
    task_queue: str,
) -> Schedule:
    """把租户组定义编译为 Temporal Schedule（纯函数，便于测试）。"""

    dispatch_request = HotNewsWindowDispatchRequest(
        tenant_group=definition.tenant_group,
        tenant_id=definition.tenant_id,
        window_minutes=definition.window_minutes,
        production_bundle_version=definition.production_bundle_version,
    )
    return Schedule(
        action=ScheduleActionStartWorkflow(
            HOT_NEWS_DISPATCH_WORKFLOW_NAME,
            dispatch_request,
            id=f"hot-news-dispatch-{definition.tenant_group}",
            task_queue=task_queue,
        ),
        spec=ScheduleSpec(
            intervals=[
                ScheduleIntervalSpec(
                    every=timedelta(minutes=definition.interval_minutes)
                )
            ]
        ),
        state=ScheduleState(
            paused=definition.paused,
            note=(
                f"hot news monitor for tenant group "
                f"{definition.tenant_group}"
            ),
        ),
    )


async def ensure_schedules(
    client: _ScheduleClient,
    definitions: tuple[HotNewsScheduleDefinition, ...],
    *,
    task_queue: str,
) -> tuple[HotNewsScheduleEnsureResult, ...]:
    """幂等地创建或更新每个租户组的 Schedule。"""

    results: list[HotNewsScheduleEnsureResult] = []
    for definition in definitions:
        schedule_id = schedule_id_for(definition.tenant_group)
        schedule = build_schedule(definition, task_queue=task_queue)
        try:
            await client.create_schedule(schedule_id, schedule)
        except ScheduleAlreadyRunningError:
            handle = client.get_schedule_handle(schedule_id)

            async def apply(
                input: ScheduleUpdateInput,
                schedule: Schedule = schedule,
            ) -> ScheduleUpdate:
                return ScheduleUpdate(schedule)

            await handle.update(apply)
            results.append(
                HotNewsScheduleEnsureResult(
                    schedule_id=schedule_id,
                    tenant_group=definition.tenant_group,
                    action="updated",
                )
            )
        else:
            results.append(
                HotNewsScheduleEnsureResult(
                    schedule_id=schedule_id,
                    tenant_group=definition.tenant_group,
                    action="created",
                )
            )
    return tuple(results)
