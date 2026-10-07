import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_data_loop_gateway_token
from app.main import create_app
from examples.local_simulation import create_local_app


PATH = "/api/v1/sql-assistant"
CONFIG = "/api/v1/local-simulation/hot-news/sql-config"
HEADERS = {
    "Authorization": "Bearer test-sql-gateway-token",
    "X-Tenant-ID": "11111111-1111-4111-8111-111111111111",
    "X-User-ID": "22222222-2222-4222-8222-222222222222",
    "X-Hot-News-Roles": "hot-news:read",
}


def test_production_does_not_expose_sql_assistant():
    assert not any(path.startswith(PATH) for path in create_app().openapi()["paths"])


def test_sql_api_requires_gateway_identity_and_read_permission(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "e2e")
    monkeypatch.setenv("LOCAL_SIMULATION_ENABLED", "1")
    app = create_local_app()
    app.dependency_overrides[get_data_loop_gateway_token] = lambda: "test-sql-gateway-token"
    client = TestClient(app)
    assert client.get(CONFIG).status_code == 401
    denied = {**HEADERS, "X-Hot-News-Roles": "not-authorized"}
    assert client.get(CONFIG, headers=denied).status_code == 403
    missing_tenant = {key: value for key, value in HEADERS.items() if key != "X-Tenant-ID"}
    assert client.get(CONFIG, headers=missing_tenant).status_code == 401
    # No lifespan/database initialized in this unit test: fail closed, not bypass.
    assert client.get(CONFIG, headers=HEADERS).status_code == 503


def test_execute_endpoint_never_accepts_user_supplied_sql(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "e2e")
    monkeypatch.setenv("LOCAL_SIMULATION_ENABLED", "1")
    app = create_local_app()
    specification = app.openapi()
    assert not any(path.startswith(PATH) for path in specification["paths"])
    properties = specification["components"]["schemas"]["SqlAssistantPreviewRequest"]["properties"]
    assert set(properties) == {"question", "scenario_id", "window_start", "window_end"}
    app.dependency_overrides[get_data_loop_gateway_token] = lambda: "test-sql-gateway-token"
    assert TestClient(app).post(PATH + "/queries/invalid-uuid/execute", headers=HEADERS).status_code == 404
