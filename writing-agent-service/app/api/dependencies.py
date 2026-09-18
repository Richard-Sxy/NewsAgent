import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import Database
from app.services.job import JobService
from app.services.orchestrator import OrchestratorService
from app.services.recovery import RecoveryService
from app.services.outbox import OutboxService
from app.services.event_reader import RedisProgressReader
from app.services.hot_news_event_stream import RedisHotNewsEventStream
from app.storage.s3 import S3ArtifactStore
from app.clients.cms import CmsPublisher
from app.services.data_loop.orchestrator import DataLoopOrchestrator
from app.services.data_loop.feedback_collector import AnalysisFeedbackCollector
from app.services.data_loop.dataset_freezer import (
    EvaluationDatasetArtifactStore,
)
from app.services.production_bundle import ProductionBundleApplicationService
from app.services.hot_news_decision import HotNewsDecisionService
from app.services.hot_news_run_store import PostgresHotNewsRunStore
from app.services.data_loop.publication_outcome import (
    PublicationOutcomeFeedbackService,
)
from app.services.production_bundle_runtime import (
    ProductionBundleRuntimeRegistry,
    UnsupportedProductionBundleRuntimeError,
)
from app.services.memory_context_application import (
    MemoryContextApplicationService,
)
from app.services.memory_promotion_application import (
    MemoryPromotionApplicationService,
)
from app.services.memory_write_application import MemoryWriteApplicationService
from app.services.hot_news_writing_handoff import (
    HotNewsWritingHandoffService,
)


class DataLoopPermission(StrEnum):
    """Least-privilege permissions asserted by the trusted API gateway."""

    READ = "data-loop:read"
    FEEDBACK_WRITE = "data-loop:feedback-write"
    LABEL_SUBMIT = "data-loop:label-submit"
    LABEL_APPROVE = "data-loop:label-approve"
    DATASET_MANAGE = "data-loop:dataset-manage"
    CANDIDATE_MANAGE = "data-loop:candidate-manage"
    RUN = "data-loop:run"
    RELEASE_APPROVE = "data-loop:release-approve"
    ROLLBACK = "data-loop:rollback"
    ADMIN = "data-loop:admin"


class HotNewsPermission(StrEnum):
    """Least-privilege permissions for the hot-news operations console.

    与 Data Loop 复用同一个网关共享 Bearer Token（DATA_LOOP_GATEWAY_TOKEN），
    但角色头独立：网关通过 ``X-Hot-News-Roles`` 注入热点模块的权限串，
    两套权限可以分别授予、分别回收。
    """

    READ = "hot-news:read"
    DECIDE = "hot-news:decide"
    HANDOFF = "hot-news:handoff"
    ADMIN = "hot-news:admin"


class MemoryPermission(StrEnum):
    """Least-privilege permissions for the user-memory control plane.

    复用 Data Loop 网关共享 Bearer Token，角色串来自独立的
    ``X-Memory-Roles`` 头，与热点、Data Loop 权限分别授予、分别回收。
    """

    READ = "memory:read"
    WRITE = "memory:write"
    APPROVE = "memory:approve"
    ADMIN = "memory:admin"


@dataclass(frozen=True, slots=True)
class DataLoopPrincipal:
    """Authenticated gateway identity used by the Data Loop control plane."""

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    permissions: frozenset[str]
    team_id: str | None = None
    section_id: str | None = None
    role_id: str | None = None


async def get_data_loop_gateway_token(request: Request) -> str | None:
    """Resolve the shared gateway token without falling back to an env read.

    The application lifespan always installs Settings on app.state. Returning
    ``None`` for an app without Settings keeps isolated test/custom app mounts
    fail-closed unless they explicitly override this dependency.
    """

    settings = getattr(request.app.state, "settings", None)
    configured = getattr(settings, "data_loop_gateway_token", None)
    if isinstance(configured, SecretStr):
        return configured.get_secret_value()
    if isinstance(configured, str):
        return configured
    return None


async def get_data_loop_runtime_registry(
    request: Request,
) -> ProductionBundleRuntimeRegistry:
    """Validate and expose the immutable deployment runtime manifest.

    Only ``ensure_supported`` is used by the API. The registry's model client
    is deliberately absent here; execution remains owned by the workers.
    """

    settings = getattr(request.app.state, "settings", None)
    manifest_json = getattr(settings, "hot_news_runtime_manifest_json", None)
    if not isinstance(manifest_json, str):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Data Loop runtime manifest is not configured",
        )
    try:
        return ProductionBundleRuntimeRegistry.validation_only(manifest_json)
    except UnsupportedProductionBundleRuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Data Loop runtime manifest is invalid or empty",
        ) from exc


async def get_data_loop_principal(
    configured_token: Annotated[
        str | None,
        Depends(get_data_loop_gateway_token),
    ],
    authorization: Annotated[str | None, Header()] = None,
    x_tenant_id: Annotated[
        uuid.UUID | None,
        Header(alias="X-Tenant-ID"),
    ] = None,
    x_user_id: Annotated[
        uuid.UUID | None,
        Header(alias="X-User-ID"),
    ] = None,
    x_data_loop_roles: Annotated[
        str | None,
        Header(alias="X-Data-Loop-Roles"),
    ] = None,
) -> DataLoopPrincipal:
    """Authenticate a request that has passed through the trusted gateway.

    The edge gateway must remove client-provided identity/role headers before
    injecting its own values. This service independently verifies the shared
    Bearer credential and never accepts tenant, user or permissions in JSON.
    """

    if configured_token is None or not configured_token.strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Data Loop gateway authentication is not configured",
        )

    parts = authorization.split() if authorization is not None else []
    if (
        len(parts) != 2
        or parts[0].lower() != "bearer"
        or not compare_digest(parts[1], configured_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid Data Loop gateway credential",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if x_tenant_id is None or x_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="trusted gateway identity headers are required",
        )

    raw_permissions = (
        [] if x_data_loop_roles is None else x_data_loop_roles.split(",")
    )
    permissions = frozenset(item.strip() for item in raw_permissions if item.strip())
    if not permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Data Loop permissions are required",
        )
    return DataLoopPrincipal(
        tenant_id=x_tenant_id,
        user_id=x_user_id,
        permissions=permissions,
    )


def require_data_loop_permission(permission: DataLoopPermission):
    """Build an endpoint dependency enforcing one minimum permission."""

    async def dependency(
        principal: Annotated[DataLoopPrincipal, Depends(get_data_loop_principal)],
    ) -> DataLoopPrincipal:
        if (
            permission.value not in principal.permissions
            and DataLoopPermission.ADMIN.value not in principal.permissions
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing Data Loop permission: {permission.value}",
            )
        return principal

    return dependency


async def get_hot_news_principal(
    configured_token: Annotated[
        str | None,
        Depends(get_data_loop_gateway_token),
    ],
    authorization: Annotated[str | None, Header()] = None,
    x_tenant_id: Annotated[
        uuid.UUID | None,
        Header(alias="X-Tenant-ID"),
    ] = None,
    x_user_id: Annotated[
        uuid.UUID | None,
        Header(alias="X-User-ID"),
    ] = None,
    x_hot_news_roles: Annotated[
        str | None,
        Header(alias="X-Hot-News-Roles"),
    ] = None,
) -> DataLoopPrincipal:
    """Authenticate a hot-news console request that passed the trusted gateway.

    共享凭据与 Data Loop 相同（DATA_LOOP_GATEWAY_TOKEN，网关注入 Bearer），
    但权限串来自独立的 ``X-Hot-News-Roles`` 头；网关必须先剥离客户端传入的
    同名头再注入自己的值。租户、用户与权限永远不从请求体获取。
    """

    if configured_token is None or not configured_token.strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="hot news gateway authentication is not configured",
        )

    parts = authorization.split() if authorization is not None else []
    if (
        len(parts) != 2
        or parts[0].lower() != "bearer"
        or not compare_digest(parts[1], configured_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid hot news gateway credential",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if x_tenant_id is None or x_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="trusted gateway identity headers are required",
        )

    raw_permissions = (
        [] if x_hot_news_roles is None else x_hot_news_roles.split(",")
    )
    permissions = frozenset(item.strip() for item in raw_permissions if item.strip())
    if not permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="hot news permissions are required",
        )
    return DataLoopPrincipal(
        tenant_id=x_tenant_id,
        user_id=x_user_id,
        permissions=permissions,
    )


def require_hot_news_permission(permission: HotNewsPermission):
    """Build an endpoint dependency enforcing one hot-news permission."""

    async def dependency(
        principal: Annotated[DataLoopPrincipal, Depends(get_hot_news_principal)],
    ) -> DataLoopPrincipal:
        if (
            permission.value not in principal.permissions
            and HotNewsPermission.ADMIN.value not in principal.permissions
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing hot news permission: {permission.value}",
            )
        return principal

    return dependency


async def get_memory_principal(
    configured_token: Annotated[
        str | None,
        Depends(get_data_loop_gateway_token),
    ],
    authorization: Annotated[str | None, Header()] = None,
    x_tenant_id: Annotated[
        uuid.UUID | None,
        Header(alias="X-Tenant-ID"),
    ] = None,
    x_user_id: Annotated[
        uuid.UUID | None,
        Header(alias="X-User-ID"),
    ] = None,
    x_memory_roles: Annotated[
        str | None,
        Header(alias="X-Memory-Roles"),
    ] = None,
    x_team_id: Annotated[str | None, Header(alias="X-Team-ID")] = None,
    x_section_id: Annotated[
        str | None,
        Header(alias="X-Section-ID"),
    ] = None,
    x_role_id: Annotated[str | None, Header(alias="X-Role-ID")] = None,
) -> DataLoopPrincipal:
    """Authenticate a user-memory request that passed the trusted gateway.

    共享凭据与 Data Loop/热点相同（DATA_LOOP_GATEWAY_TOKEN），但权限串来自
    独立的 ``X-Memory-Roles`` 头。租户、用户与组织作用域只信任网关注入的
    Header，绝不从请求体读取，防止越权写入他人记忆。
    """

    if configured_token is None or not configured_token.strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="memory gateway authentication is not configured",
        )

    parts = authorization.split() if authorization is not None else []
    if (
        len(parts) != 2
        or parts[0].lower() != "bearer"
        or not compare_digest(parts[1], configured_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid memory gateway credential",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if x_tenant_id is None or x_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="trusted gateway identity headers are required",
        )

    raw_permissions = (
        [] if x_memory_roles is None else x_memory_roles.split(",")
    )
    permissions = frozenset(item.strip() for item in raw_permissions if item.strip())
    if not permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="memory permissions are required",
        )
    return DataLoopPrincipal(
        tenant_id=x_tenant_id,
        user_id=x_user_id,
        permissions=permissions,
        team_id=_optional_header_value("X-Team-ID", x_team_id),
        section_id=_optional_header_value("X-Section-ID", x_section_id),
        role_id=_optional_header_value("X-Role-ID", x_role_id),
    )


def _optional_header_value(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{name} must not be blank when provided",
        )
    return normalized


def require_memory_permission(permission: MemoryPermission):
    """Build an endpoint dependency enforcing one memory permission."""

    async def dependency(
        principal: Annotated[DataLoopPrincipal, Depends(get_memory_principal)],
    ) -> DataLoopPrincipal:
        if (
            permission.value not in principal.permissions
            and MemoryPermission.ADMIN.value not in principal.permissions
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing memory permission: {permission.value}",
            )
        return principal

    return dependency


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


async def get_hot_news_event_stream(
    request: Request,
) -> RedisHotNewsEventStream:
    return request.app.state.hot_news_event_stream


async def get_orchestrator(request: Request) -> OrchestratorService:
    return request.app.state.orchestrator


async def get_data_loop_orchestrator(request: Request) -> DataLoopOrchestrator:
    return request.app.state.data_loop_orchestrator


async def get_analysis_feedback_collector() -> AnalysisFeedbackCollector:
    return AnalysisFeedbackCollector()


async def get_evaluation_dataset_artifact_store(
    request: Request,
) -> EvaluationDatasetArtifactStore:
    return request.app.state.evaluation_dataset_artifact_store


async def get_production_bundle_service() -> ProductionBundleApplicationService:
    return ProductionBundleApplicationService()


async def get_hot_news_decision_service(
    request: Request,
) -> HotNewsDecisionService:
    database: Database = request.app.state.database
    return HotNewsDecisionService(
        database=database,
        memory_store=PostgresHotNewsRunStore(database),
    )


async def get_publication_outcome_feedback_service(
    request: Request,
) -> PublicationOutcomeFeedbackService:
    database: Database = request.app.state.database
    return PublicationOutcomeFeedbackService(
        memory_store=PostgresHotNewsRunStore(database),
    )


async def get_hot_news_query_service(request: Request):
    """热点控制台的只读查询服务；延迟导入避免 API 启动环路。"""

    from app.services.hot_news_query import HotNewsQueryService

    database: Database = request.app.state.database
    return HotNewsQueryService(database=database)


async def get_hot_news_writing_handoff_service(
    request: Request,
) -> HotNewsWritingHandoffService:
    """把热点分析快照转交写作流程的服务；复用主写作编排器。"""

    database: Database = request.app.state.database
    orchestrator: OrchestratorService = request.app.state.orchestrator
    return HotNewsWritingHandoffService(
        run_store=PostgresHotNewsRunStore(database),
        job_service=JobService(OutboxService()),
        orchestrator=orchestrator,
    )


async def get_memory_write_service() -> MemoryWriteApplicationService:
    return MemoryWriteApplicationService()


async def get_memory_promotion_service() -> MemoryPromotionApplicationService:
    return MemoryPromotionApplicationService()


async def get_memory_context_service() -> MemoryContextApplicationService:
    return MemoryContextApplicationService()


async def get_artifact_store(request: Request) -> S3ArtifactStore:
    return request.app.state.artifact_store


async def get_cms_publisher(request: Request) -> CmsPublisher:
    return request.app.state.cms_publisher


async def get_tenant_id(x_tenant_id: uuid.UUID = Header(alias="X-Tenant-ID")) -> uuid.UUID:
    """仅用于服务间可信 Header；生产环境应由网关注入。"""
    return x_tenant_id


async def get_user_id(x_user_id: uuid.UUID = Header(alias="X-User-ID")) -> uuid.UUID:
    return x_user_id
