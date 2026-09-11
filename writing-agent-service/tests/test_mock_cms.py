"""Mock CMS 发布网关的契约测试。

企业 CMS 暂未提供，deploy/mock-cms 是写作发布链路的本地替身。
这里验证：鉴权、必需头、幂等重放、幂等键冲突，以及
``CmsPublisher`` 与 Mock CMS 的真实 HTTP 契约（经 ASGI Transport，
不访问网络）。
"""

import importlib.util
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.clients.cms import CmsPublisher

MOCK_CMS_PATH = (
    Path(__file__).resolve().parents[1] / "deploy" / "mock-cms" / "app.py"
)

TENANT_ID = "11111111-1111-4111-8111-111111111111"
JOB_ID = "22222222-2222-4222-8222-222222222222"
TOKEN = "mock-cms-dev-token"


def load_mock_cms(monkeypatch, publications=None):
    """以独立模块名加载 Mock CMS 应用，避免测试间共享内存状态。"""

    module_name = f"mock_cms_under_test_{uuid4_hex()}"
    spec = importlib.util.spec_from_file_location(module_name, MOCK_CMS_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    if publications is not None:
        monkeypatch.setattr(module, "_PUBLICATIONS", publications)
    return module


def uuid4_hex() -> str:
    return uuid.uuid4().hex


def make_client(module, *, token: str | None = TOKEN) -> TestClient:
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return TestClient(module.app, headers=headers)


def publish_payload() -> dict:
    return {
        "job_id": JOB_ID,
        "channel": "headline",
        "article": {"title": "测试终稿", "body": "正文"},
    }


def publish_headers() -> dict:
    return {
        "Idempotency-Key": f"news-writing:{TENANT_ID}:{JOB_ID}:headline",
        "X-Tenant-ID": TENANT_ID,
    }


def test_publish_success(monkeypatch) -> None:
    module = load_mock_cms(monkeypatch, publications={})

    response = make_client(module).post(
        "/api/v1/publications",
        json=publish_payload(),
        headers=publish_headers(),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["publication_id"].startswith("mock-cms-")
    assert body["status"] == "published"
    assert body["replay"] is False


def test_publish_rejects_missing_token(monkeypatch) -> None:
    module = load_mock_cms(monkeypatch, publications={})

    response = make_client(module, token=None).post(
        "/api/v1/publications",
        json=publish_payload(),
        headers=publish_headers(),
    )

    assert response.status_code == 401


def test_publish_rejects_wrong_token(monkeypatch) -> None:
    module = load_mock_cms(monkeypatch, publications={})

    response = make_client(module, token="wrong-token").post(
        "/api/v1/publications",
        json=publish_payload(),
        headers=publish_headers(),
    )

    assert response.status_code == 401


def test_publish_requires_idempotency_key(monkeypatch) -> None:
    module = load_mock_cms(monkeypatch, publications={})
    headers = publish_headers()
    del headers["Idempotency-Key"]

    response = make_client(module).post(
        "/api/v1/publications",
        json=publish_payload(),
        headers=headers,
    )

    assert response.status_code == 422


def test_publish_requires_tenant_header(monkeypatch) -> None:
    module = load_mock_cms(monkeypatch, publications={})
    headers = publish_headers()
    del headers["X-Tenant-ID"]

    response = make_client(module).post(
        "/api/v1/publications",
        json=publish_payload(),
        headers=headers,
    )

    assert response.status_code == 422


def test_publish_idempotent_replay(monkeypatch) -> None:
    module = load_mock_cms(monkeypatch, publications={})
    client = make_client(module)

    first = client.post(
        "/api/v1/publications",
        json=publish_payload(),
        headers=publish_headers(),
    )
    second = client.post(
        "/api/v1/publications",
        json=publish_payload(),
        headers=publish_headers(),
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["replay"] is True
    assert (
        second.json()["publication_id"] == first.json()["publication_id"]
    )
    assert len(module._PUBLICATIONS) == 1


def test_publish_same_key_different_content_conflicts(monkeypatch) -> None:
    module = load_mock_cms(monkeypatch, publications={})
    client = make_client(module)
    client.post(
        "/api/v1/publications",
        json=publish_payload(),
        headers=publish_headers(),
    )

    response = client.post(
        "/api/v1/publications",
        json={**publish_payload(), "channel": "other-channel"},
        headers=publish_headers(),
    )

    assert response.status_code == 409


def test_list_and_get_publications(monkeypatch) -> None:
    module = load_mock_cms(monkeypatch, publications={})
    client = make_client(module)
    created = client.post(
        "/api/v1/publications",
        json=publish_payload(),
        headers=publish_headers(),
    ).json()

    listed = client.get("/api/v1/publications")
    assert listed.status_code == 200
    assert listed.json()[0]["publication_id"] == created["publication_id"]
    assert listed.json()[0]["tenant_id"] == TENANT_ID

    detail = client.get(
        f"/api/v1/publications/{created['publication_id']}"
    )
    assert detail.status_code == 200
    assert detail.json()["job_id"] == JOB_ID

    missing = client.get("/api/v1/publications/mock-cms-does-not-exist")
    assert missing.status_code == 404


def test_cms_publisher_against_mock_cms(monkeypatch) -> None:
    """CmsPublisher 与 Mock CMS 的真实契约：发布成功且幂等重放一致。"""

    module = load_mock_cms(monkeypatch, publications={})
    transport = httpx.ASGITransport(app=module.app)

    real_async_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(
        "app.clients.cms.httpx.AsyncClient", client_factory
    )

    settings = SimpleNamespace(
        cms_publish_url="http://mock-cms.test/api/v1/publications",
        cms_publish_token=TOKEN,
        cms_publish_timeout_seconds=5.0,
    )
    publisher = CmsPublisher(settings)

    tenant_id = uuid.UUID(TENANT_ID)
    job_id = uuid.UUID(JOB_ID)

    async def run():
        first = await publisher.publish(
            job_id=job_id,
            tenant_id=tenant_id,
            channel="headline",
            article={"title": "测试终稿"},
        )
        second = await publisher.publish(
            job_id=job_id,
            tenant_id=tenant_id,
            channel="headline",
            article={"title": "测试终稿"},
        )
        return first, second

    import asyncio

    first, second = asyncio.run(run())

    assert first.startswith("mock-cms-")
    assert second == first
    # 同一 Idempotency-Key 只产生一条发布记录
    assert len(module._PUBLICATIONS) == 1
    record = next(iter(module._PUBLICATIONS.values()))
    assert record.job_id == JOB_ID
    assert record.tenant_id == TENANT_ID
