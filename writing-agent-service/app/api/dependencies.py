import uuid
from collections.abc import AsyncIterator

from fastapi import Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import Database
from app.services.job import JobService
from app.services.orchestrator import OrchestratorService
from app.services.recovery import RecoveryService
from app.services.outbox import OutboxService
from app.services.event_reader import RedisProgressReader
from app.storage.s3 import S3ArtifactStore
from app.clients.cms import CmsPublisher

""" 这边配置提供：数据库Session/Temporal Client/OrchestratorService/当前 """
async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    database: Database = request.app.state.database
    async with database.session() as session:
        yield session            # 把 session 交出去，等待执行完成后结束后自动清理


async def get_job_service() -> JobService:
    return JobService(OutboxService())


async def get_recovery_service() -> RecoveryService:
    return RecoveryService(OutboxService())


async def get_event_reader(request: Request) -> RedisProgressReader:
    return request.app.state.event_reader


async def get_orchestrator(request: Request) -> OrchestratorService:
    return request.app.state.orchestrator


async def get_artifact_store(request: Request) -> S3ArtifactStore:
    return request.app.state.artifact_store


async def get_cms_publisher(request: Request) -> CmsPublisher:
    return request.app.state.cms_publisher


async def get_tenant_id(x_tenant_id: uuid.UUID = Header(alias="X-Tenant-ID")) -> uuid.UUID:
    """仅用于服务间可信 Header；生产环境应由网关注入。"""
    return x_tenant_id


async def get_user_id(x_user_id: uuid.UUID = Header(alias="X-User-ID")) -> uuid.UUID:
    return x_user_id
