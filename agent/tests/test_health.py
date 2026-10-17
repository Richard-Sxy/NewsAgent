from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.main import app, health, metrics, ready


class FakeDatabase:
    def __init__(self, session) -> None:
        self.session_value = session

    @asynccontextmanager
    async def session(self):
        yield self.session_value


def request_with(*, database, redis, temporal):
    state = SimpleNamespace(database=database, redis=redis, temporal=temporal)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_liveness_does_not_depend_on_external_services() -> None:
    assert health() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readiness_reports_all_critical_dependencies() -> None:
    session = AsyncMock()
    redis = AsyncMock()
    temporal = SimpleNamespace(
        service_client=SimpleNamespace(check_health=AsyncMock())
    )

    response = await ready(
        request_with(
            database=FakeDatabase(session),
            redis=redis,
            temporal=temporal,
        )
    )

    assert response.status_code == 200
    assert b'"status":"ready"' in response.body


@pytest.mark.asyncio
async def test_readiness_returns_503_when_dependency_is_down() -> None:
    session = AsyncMock()
    redis = AsyncMock()
    redis.ping.side_effect = ConnectionError("redis down")
    temporal = SimpleNamespace(
        service_client=SimpleNamespace(check_health=AsyncMock())
    )

    response = await ready(
        request_with(
            database=FakeDatabase(session),
            redis=redis,
            temporal=temporal,
        )
    )

    assert response.status_code == 503
    assert b'"redis":"error"' in response.body


@pytest.mark.asyncio
async def test_metrics_exposes_business_and_outbox_gauges() -> None:
    session = AsyncMock()
    session.execute.side_effect = [
        [("final_approved", 2), ("published", 3)],
        [("pending", 1), ("dead_letter", 0)],
    ]
    request = request_with(
        database=FakeDatabase(session),
        redis=AsyncMock(),
        temporal=AsyncMock(),
    )
    response = await metrics(request)
    assert b'news_agent_jobs{status="published"} 3' in response.body
    assert b'news_agent_outbox_events{status="pending"} 1' in response.body
