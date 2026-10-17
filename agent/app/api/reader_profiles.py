"""Authenticated tenant-scoped, bounded aggregate reader-profile queries."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.api.dependencies import DataLoopPrincipal, HotNewsPermission, require_hot_news_permission
from app.modules.reader_profiles.contracts import Identifier, ReaderProfileProviderError, ReaderProfileQuery

router = APIRouter(prefix="/api/v1/reader-profiles", tags=["news-reader-profiles"])
ReadPrincipal = Annotated[DataLoopPrincipal, Depends(require_hot_news_permission(HotNewsPermission.READ))]


class ReaderProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    news_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=20)
    window_start: AwareDatetime
    window_end: AwareDatetime


def get_service(request: Request):
    service = getattr(request.app.state, "reader_profile_service", None)
    if service is None:
        raise HTTPException(503, "新闻访客画像服务未启用。")
    return service


@router.post("/query")
async def query_profiles(body: ReaderProfileRequest, principal: ReadPrincipal, response: Response,
                         service=Depends(get_service)):
    response.headers["Cache-Control"] = "no-store"
    try:
        query = ReaderProfileQuery(tenant_id=principal.tenant_id, news_ids=body.news_ids,
            window_start=body.window_start, window_end=body.window_end,
            source_release_sha256=service.source_release_sha256)
        batch = await service.fetch(query)
    except ValueError as exc:
        raise HTTPException(422, "新闻或窗口参数不符合访客画像查询约束。") from exc
    except ReaderProfileProviderError as exc:
        status = 422 if exc.code == "invalid_query" else 503
        raise HTTPException(status, "访客画像暂不可用或窗口尚未完成；本次未生成有效人群结论。") from exc
    return {"snapshot": batch, "dispatch_enabled": False, "manual_review_required": True}
