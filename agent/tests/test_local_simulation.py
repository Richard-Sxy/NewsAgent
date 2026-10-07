"""The local trigger must not become a production or unauthenticated API."""

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_data_loop_gateway_token
from app.main import create_app
from examples.local_simulation import create_local_app


PATH = "/api/v1/local-simulation/hot-news/run"
TOKEN = "test-local-simulation-token"


def test_production_app_does_not_mount_local_trigger() -> None:
    assert PATH not in create_app().openapi()["paths"]


def test_local_app_exposes_sql_only_as_hot_news_tool(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "e2e")
    monkeypatch.setenv("LOCAL_SIMULATION_ENABLED", "1")
    paths = create_local_app().openapi()["paths"]
    assert "/api/v1/local-simulation/hot-news/sql-config" in paths
    assert not any(path.startswith("/api/v1/sql-assistant") for path in paths)


@pytest.mark.parametrize(
    ("environment", "enabled"),
    [("production", "1"), ("e2e", "0")],
)
def test_local_factory_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    enabled: str,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("LOCAL_SIMULATION_ENABLED", enabled)
    with pytest.raises(RuntimeError, match="only available in the E2E stack"):
        create_local_app()


def test_local_trigger_requires_admin_and_scenario_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "e2e")
    monkeypatch.setenv("LOCAL_SIMULATION_ENABLED", "1")
    app = create_local_app()
    app.dependency_overrides[get_data_loop_gateway_token] = lambda: TOKEN
    client = TestClient(app)

    assert client.post(PATH).status_code == 401
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "X-Tenant-ID": "11111111-1111-4111-8111-111111111111",
        "X-User-ID": "22222222-2222-4222-8222-222222222222",
        "X-Hot-News-Roles": "hot-news:read",
    }
    assert client.post(PATH, headers=headers).status_code == 403
    headers["X-Hot-News-Roles"] = "hot-news:admin"
    headers["X-Tenant-ID"] = "33333333-3333-4333-8333-333333333333"
    assert client.post(PATH, headers=headers).status_code == 403


def test_local_hot_news_rejects_multi_hour_window_before_any_io(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "e2e")
    monkeypatch.setenv("LOCAL_SIMULATION_ENABLED", "1")
    app = create_local_app()
    app.dependency_overrides[get_data_loop_gateway_token] = lambda: TOKEN
    response = TestClient(app).post(PATH, headers={
        "Authorization": f"Bearer {TOKEN}",
        "X-Tenant-ID": "11111111-1111-4111-8111-111111111111",
        "X-User-ID": "22222222-2222-4222-8222-222222222222",
        "X-Hot-News-Roles": "hot-news:admin",
    }, json={"question": "点击前5条新闻", "scenario_id": "news-ranking",
             "window_start": "2026-10-03T00:00:00+08:00", "window_end": "2026-10-04T00:00:00+08:00"})
    assert response.status_code == 422
    assert "恰好1小时" in response.json()["detail"]
