import uuid
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.dependencies import (
    DataLoopPermission,
    DataLoopPrincipal,
    WritingPermission,
    get_data_loop_gateway_token,
    require_data_loop_permission,
    require_writing_permission,
)


TEST_TOKEN = "test-writing-gateway-token"
TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()


def writing_app(configured_token=TEST_TOKEN):
    app = FastAPI()
    app.dependency_overrides[get_data_loop_gateway_token] = lambda: configured_token

    @app.get("/writing/read")
    async def read(
        principal: Annotated[
            DataLoopPrincipal, Depends(require_writing_permission(WritingPermission.READ))
        ],
    ):
        return {"tenant_id": str(principal.tenant_id), "user_id": str(principal.user_id)}

    @app.get("/writing/admin")
    async def admin(
        principal: Annotated[
            DataLoopPrincipal, Depends(require_writing_permission(WritingPermission.ADMIN))
        ],
    ):
        return {"permissions": sorted(principal.permissions)}

    return app


def headers(**overrides):
    return {
        "Authorization": f"Bearer {TEST_TOKEN}",
        "X-Tenant-ID": str(TENANT_ID),
        "X-User-ID": str(USER_ID),
        "X-Writing-Roles": WritingPermission.READ.value,
    } | overrides


@pytest.mark.parametrize("role", ["writing:read", "writing:admin", " writing:read,writing:read "])
def test_writing_read_uses_same_gateway_and_module_roles(role):
    with TestClient(writing_app()) as client:
        response = client.get("/writing/read", headers=headers(**{"X-Writing-Roles": role}))
    assert response.status_code == 200
    assert response.json() == {"tenant_id": str(TENANT_ID), "user_id": str(USER_ID)}


def test_writing_admin_is_required_for_future_start_hooks():
    with TestClient(writing_app()) as client:
        denied = client.get("/writing/admin", headers=headers())
        allowed = client.get("/writing/admin", headers=headers(**{"X-Writing-Roles": "writing:admin"}))
    assert denied.status_code == 403
    assert allowed.status_code == 200


@pytest.mark.parametrize(
    "overrides,removed,expected",
    [
        ({"Authorization": "Bearer wrong-token"}, None, 401),
        ({"Authorization": "Basic invalid"}, None, 401),
        ({}, "Authorization", 401),
        ({}, "X-Tenant-ID", 401),
        ({}, "X-User-ID", 401),
        ({}, "X-Writing-Roles", 403),
        ({"X-Writing-Roles": "  ,  "}, None, 403),
        ({"X-Writing-Roles": "hot-news:admin"}, None, 403),
        ({"X-Writing-Roles": "data-loop:admin"}, None, 403),
        ({"X-Hot-News-Roles": "writing:admin", "X-Data-Loop-Roles": "writing:admin"}, "X-Writing-Roles", 403),
    ],
)
def test_writing_permission_fails_closed(overrides, removed, expected):
    request_headers = headers(**overrides)
    if removed:
        del request_headers[removed]
    with TestClient(writing_app()) as client:
        response = client.get("/writing/read", headers=request_headers)
    assert response.status_code == expected


@pytest.mark.parametrize("configured_token", [None, "", "  "])
def test_writing_gateway_must_be_configured(configured_token):
    with TestClient(writing_app(configured_token)) as client:
        response = client.get("/writing/read", headers=headers())
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_data_loop_admin_remains_independent_and_uses_existing_enum():
    assert DataLoopPermission.ADMIN.value == "data-loop:admin"
    principal = DataLoopPrincipal(TENANT_ID, USER_ID, frozenset({"data-loop:admin"}))
    assert await require_data_loop_permission(DataLoopPermission.READ)(principal) is principal
    with pytest.raises(HTTPException) as denied:
        await require_writing_permission(WritingPermission.READ)(principal)
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_writing_admin_does_not_grant_data_loop_permissions():
    principal = DataLoopPrincipal(TENANT_ID, USER_ID, frozenset({"writing:admin"}))
    with pytest.raises(HTTPException) as denied:
        await require_data_loop_permission(DataLoopPermission.RUN)(principal)
    assert denied.value.status_code == 403
