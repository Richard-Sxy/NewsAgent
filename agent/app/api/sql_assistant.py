"""独立 SQL 助手接口定义。包含配置读取、查询浏览和执行；当前主应用没有挂载这个路由，文件存在不等于接口正在开放。"""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import DataLoopPrincipal, HotNewsPermission, require_hot_news_permission
from app.domain.errors import AgentOutputValidationError, Text2SqlGuardError
from app.schemas.sql_assistant import SqlAssistantPreview, SqlAssistantPreviewRequest, SqlAssistantResult
from app.sql_assistant.planner import SqlAssistantQuestionError
from app.sql_assistant.service import SqlAssistantError, SqlAssistantService
from app.sql_assistant.hot_news_binding import HotNewsSqlBindingError, HotNewsSqlBindingPersistenceError


router = APIRouter(prefix="/api/v1/sql-assistant", tags=["sql-assistant-local"])
ReadPrincipal = Annotated[DataLoopPrincipal, Depends(require_hot_news_permission(HotNewsPermission.READ))]


def service(request: Request) -> SqlAssistantService:
    value = getattr(request.app.state, "sql_assistant", None)
    if value is None:
        raise HTTPException(status_code=503, detail="本地 SQL 模拟服务未初始化")
    return value


def api_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (SqlAssistantError, HotNewsSqlBindingError, HotNewsSqlBindingPersistenceError)):
        return HTTPException(exc.status_code, detail=str(exc))
    if isinstance(exc, (SqlAssistantQuestionError, Text2SqlGuardError, AgentOutputValidationError)):
        return HTTPException(422, detail=str(exc)[:1000])
    if isinstance(exc, SQLAlchemyError):
        return HTTPException(503, detail="查询审计存储暂不可用")
    return HTTPException(503, detail="Text2SQL 配置或数仓格式校验失败")


@router.get("/config")
async def get_config(request: Request, principal: ReadPrincipal) -> dict:
    try:
        return service(request).config()
    except (ValueError, OSError, RuntimeError) as exc:
        raise api_error(exc) from exc


@router.post("/preview", response_model=SqlAssistantPreview)
async def preview_query(body: SqlAssistantPreviewRequest, request: Request, principal: ReadPrincipal) -> SqlAssistantPreview:
    try:
        return await service(request).preview(body, tenant_id=str(principal.tenant_id), user_id=str(principal.user_id))
    except (ValueError, RuntimeError, SQLAlchemyError, OSError) as exc:
        raise api_error(exc) from exc


@router.post("/queries/{query_id}/execute", response_model=SqlAssistantResult)
async def execute_query(query_id: UUID, request: Request, principal: ReadPrincipal) -> SqlAssistantResult:
    try:
        return await service(request).execute(str(query_id), tenant_id=str(principal.tenant_id), user_id=str(principal.user_id))
    except (ValueError, RuntimeError, SQLAlchemyError, OSError) as exc:
        raise api_error(exc) from exc
