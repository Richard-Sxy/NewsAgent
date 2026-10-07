"""NewsAgent 本地/测试用 Mock CMS 发布网关。

企业 CMS 暂未提供，本服务模拟其发布契约，供写作链路
（``POST /api/v1/jobs/{job_id}/publish`` → ``CmsPublisher``）端到端联调：

- ``POST /api/v1/publications``：发布文章。必须携带
  ``Idempotency-Key``、``X-Tenant-ID`` 与 ``Authorization: Bearer`` 头；
  同一幂等键重放返回同一个 ``publication_id``，不产生重复发布。
- ``GET  /api/v1/publications``：列出全部发布记录（验收用，按时间倒序）。
- ``GET  /api/v1/publications/{publication_id}``：单条发布回执。
- ``GET  /healthz``：健康检查（无需鉴权）。

仅用于本地与测试环境；不持久化，进程重启后记录清空。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from secrets import compare_digest
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

MOCK_CMS_TOKEN = os.environ.get("MOCK_CMS_TOKEN", "mock-cms-dev-token")


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=64)
    channel: str = Field(min_length=1, max_length=64)
    article: dict[str, Any]


class PublicationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publication_id: str
    tenant_id: str
    job_id: str
    channel: str
    idempotency_key: str
    article: dict[str, Any]
    published_at: datetime


class PublishResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publication_id: str
    status: str
    replay: bool


# (tenant_id, idempotency_key) -> PublicationRecord
_PUBLICATIONS: dict[tuple[str, str], PublicationRecord] = {}


async def require_mock_cms_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    parts = authorization.split() if authorization is not None else []
    if (
        len(parts) != 2
        or parts[0].lower() != "bearer"
        or not compare_digest(parts[1], MOCK_CMS_TOKEN)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid mock CMS credential",
            headers={"WWW-Authenticate": "Bearer"},
        )


def create_app() -> FastAPI:
    app = FastAPI(title="NewsAgent Mock CMS", version="0.1.0")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/api/v1/publications",
        response_model=PublishResponse,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_mock_cms_token)],
    )
    async def publish(
        request: PublishRequest,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key")
        ] = None,
        x_tenant_id: Annotated[
            str | None, Header(alias="X-Tenant-ID")
        ] = None,
    ) -> PublishResponse:
        if not idempotency_key or not idempotency_key.strip():
            raise HTTPException(
                status_code=422,
                detail="Idempotency-Key header is required",
            )
        if not x_tenant_id or not x_tenant_id.strip():
            raise HTTPException(
                status_code=422,
                detail="X-Tenant-ID header is required",
            )

        key = (x_tenant_id, idempotency_key)
        existing = _PUBLICATIONS.get(key)
        if existing is not None:
            if (
                existing.job_id != request.job_id
                or existing.channel != request.channel
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "idempotency key is already bound to different content"
                    ),
                )
            return PublishResponse(
                publication_id=existing.publication_id,
                status="published",
                replay=True,
            )

        record = PublicationRecord(
            publication_id=f"mock-cms-{uuid.uuid4()}",
            tenant_id=x_tenant_id,
            job_id=request.job_id,
            channel=request.channel,
            idempotency_key=idempotency_key,
            article=request.article,
            published_at=datetime.now(timezone.utc),
        )
        _PUBLICATIONS[key] = record
        return PublishResponse(
            publication_id=record.publication_id,
            status="published",
            replay=False,
        )

    @app.get(
        "/api/v1/publications",
        response_model=list[PublicationRecord],
        dependencies=[Depends(require_mock_cms_token)],
    )
    async def list_publications() -> list[PublicationRecord]:
        return sorted(
            _PUBLICATIONS.values(),
            key=lambda item: item.published_at,
            reverse=True,
        )

    @app.get(
        "/api/v1/publications/{publication_id}",
        response_model=PublicationRecord,
        dependencies=[Depends(require_mock_cms_token)],
    )
    async def get_publication(publication_id: str) -> PublicationRecord:
        for record in _PUBLICATIONS.values():
            if record.publication_id == publication_id:
                return record
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="publication not found",
        )

    return app


app = create_app()
