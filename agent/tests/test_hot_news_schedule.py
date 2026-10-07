"""热点监控 Schedule 管理的单元测试。

不连接真实 Temporal：配置解析与 Schedule 编译是纯函数；
ensure 流程使用伪客户端验证 create / already-exists-update 两条路径。
"""

import asyncio
from datetime import timedelta

import pytest
from temporalio.client import ScheduleAlreadyRunningError

from app.services.hot_news_schedule import (
    HotNewsScheduleConfigError,
    HotNewsScheduleDefinition,
    build_schedule,
    ensure_schedules,
    parse_schedule_definitions,
    schedule_id_for,
)
from app.workflows.contracts import HotNewsWindowDispatchRequest
from app.workflows.hot_news import HOT_NEWS_DISPATCH_WORKFLOW_NAME

TASK_QUEUE = "hot-news"


def definition(**overrides) -> HotNewsScheduleDefinition:
    values = {
        "tenant_group": "group-a",
        "tenant_id": "11111111-1111-4111-8111-111111111111",
        "production_bundle_version": "bundle-2026-09-10",
        "window_minutes": 60,
        "interval_minutes": 30,
    }
    values.update(overrides)
    return HotNewsScheduleDefinition(**values)


def test_schedule_id_is_stable_per_tenant_group() -> None:
    assert (
        schedule_id_for("group-a")
        == schedule_id_for("group-a")
        == "hot-news-monitor-group-a"
    )
    assert schedule_id_for("group-b") != schedule_id_for("group-a")


def test_schedule_id_rejects_bad_slug() -> None:
    with pytest.raises(HotNewsScheduleConfigError):
        schedule_id_for("Group_A")


def test_parse_definitions_from_json() -> None:
    raw = (
        '[{"tenant_group":"group-a","tenant_id":"t-1",'
        '"production_bundle_version":"b-1","window_minutes":60,'
        '"interval_minutes":30}]'
    )
    definitions = parse_schedule_definitions(raw)
    assert len(definitions) == 1
    assert definitions[0].tenant_group == "group-a"
    assert definitions[0].paused is False


def test_parse_definitions_rejects_invalid_json() -> None:
    with pytest.raises(HotNewsScheduleConfigError):
        parse_schedule_definitions("{not json")


def test_parse_definitions_rejects_non_array() -> None:
    with pytest.raises(HotNewsScheduleConfigError):
        parse_schedule_definitions('{"tenant_group":"a"}')


def test_parse_definitions_rejects_duplicate_tenant_group() -> None:
    raw = (
        '[{"tenant_group":"group-a","tenant_id":"t-1",'
        '"production_bundle_version":"b-1","window_minutes":60,'
        '"interval_minutes":30},'
        '{"tenant_group":"group-a","tenant_id":"t-2",'
        '"production_bundle_version":"b-1","window_minutes":60,'
        '"interval_minutes":30}]'
    )
    with pytest.raises(HotNewsScheduleConfigError, match="重复"):
        parse_schedule_definitions(raw)


def test_parse_definitions_rejects_bad_field() -> None:
    raw = (
        '[{"tenant_group":"group-a","tenant_id":"t-1",'
        '"production_bundle_version":"b-1","window_minutes":1,'
        '"interval_minutes":30}]'
    )
    with pytest.raises(HotNewsScheduleConfigError):
        parse_schedule_definitions(raw)


def test_parse_empty_json_means_no_schedules() -> None:
    assert parse_schedule_definitions("") == ()
    assert parse_schedule_definitions("[]") == ()


def test_build_schedule_compiles_dispatch_action() -> None:
    schedule = build_schedule(definition(), task_queue=TASK_QUEUE)

    action = schedule.action
    assert action.workflow == HOT_NEWS_DISPATCH_WORKFLOW_NAME
    assert action.id == "hot-news-dispatch-group-a"
    assert action.task_queue == TASK_QUEUE
    request = action.args[0]
    assert isinstance(request, HotNewsWindowDispatchRequest)
    assert request.tenant_group == "group-a"
    assert request.window_minutes == 60
    assert request.production_bundle_version == "bundle-2026-09-10"

    assert len(schedule.spec.intervals) == 1
    assert schedule.spec.intervals[0].every == timedelta(minutes=30)
    assert schedule.state.paused is False


def test_build_schedule_honors_paused_flag() -> None:
    schedule = build_schedule(
        definition(paused=True), task_queue=TASK_QUEUE
    )
    assert schedule.state.paused is True


class FakeHandle:
    def __init__(self, client, schedule_id):
        self._client = client
        self._id = schedule_id

    async def update(self, updater):
        update = await updater(None)
        self._client.updated[self._id] = update.schedule


class FakeClient:
    def __init__(self, *, already_exists: bool):
        self.already_exists = already_exists
        self.created: dict = {}
        self.updated: dict = {}

    async def create_schedule(self, id, schedule):
        if self.already_exists:
            raise ScheduleAlreadyRunningError()
        self.created[id] = schedule

    def get_schedule_handle(self, id):
        return FakeHandle(self, id)


def test_ensure_creates_missing_schedules() -> None:
    client = FakeClient(already_exists=False)
    definitions = (definition(), definition(tenant_group="group-b"))

    results = asyncio.run(
        ensure_schedules(client, definitions, task_queue=TASK_QUEUE)
    )

    assert [r.action for r in results] == ["created", "created"]
    assert set(client.created) == {
        "hot-news-monitor-group-a",
        "hot-news-monitor-group-b",
    }
    assert client.updated == {}


def test_ensure_updates_existing_schedules() -> None:
    client = FakeClient(already_exists=True)

    results = asyncio.run(
        ensure_schedules(client, (definition(),), task_queue=TASK_QUEUE)
    )

    assert [r.action for r in results] == ["updated"]
    assert client.created == {}
    assert "hot-news-monitor-group-a" in client.updated
    updated = client.updated["hot-news-monitor-group-a"]
    assert updated.action.id == "hot-news-dispatch-group-a"
