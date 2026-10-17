"""Read-only enterprise topic and audience preview, never recipient delivery."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response

from app.api.dependencies import DataLoopPrincipal, HotNewsPermission, require_hot_news_permission
from app.modules.audience.contracts import AudienceProviderError, AudienceSelection
from app.modules.audience.service import (
    AudiencePreview, AudiencePreviewService, AudienceSelectionError,
    AudienceSnapshotError, AudienceTargetNotFound,
)
from app.domain.errors import HotNewsPersistenceError

router = APIRouter(prefix="/api/v1/audience", tags=["enterprise-audience"])
ReadPrincipal = Annotated[DataLoopPrincipal, Depends(require_hot_news_permission(HotNewsPermission.READ))]
DecidePrincipal = Annotated[DataLoopPrincipal, Depends(require_hot_news_permission(HotNewsPermission.DECIDE))]


def get_audience_service(request: Request) -> AudiencePreviewService:
    service = getattr(request.app.state, "audience_service", None)
    if service is None:
        raise HTTPException(503, "主题受众服务未启用，请联系管理员配置企业接口或本地模拟。")
    return service


@router.get("/topics")
async def topics(principal: ReadPrincipal, response: Response,
                 service: AudiencePreviewService = Depends(get_audience_service)):
    response.headers["Cache-Control"] = "no-store"
    return service.topics()


@router.post("/runs/{run_id}/news/{news_id}/preview", response_model=AudiencePreview)
async def preview(run_id: UUID,
                  news_id: Annotated[str, Path(min_length=1, max_length=128)],
                  selection: AudienceSelection, principal: DecidePrincipal, response: Response,
                  service: AudiencePreviewService = Depends(get_audience_service)):
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.preview(tenant_id=principal.tenant_id, run_id=run_id,
                                     news_id=news_id, selection=selection)
    except AudienceSelectionError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except AudienceTargetNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except AudienceSnapshotError as exc:
        raise HTTPException(502, str(exc)) from exc
    except AudienceProviderError as exc:
        raise HTTPException(503, "企业受众服务暂不可用，本次没有生成有效预估。") from exc
    except HotNewsPersistenceError as exc:
        raise HTTPException(503, "暂时无法读取热点分析，本次没有查询受众。") from exc
