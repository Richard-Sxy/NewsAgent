"""Tenant-authorized, read-only searches of local aggregate labels."""

from datetime import timedelta
from typing import Annotated, Literal, Self

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.api.dependencies import DataLoopPrincipal, HotNewsPermission, require_hot_news_permission
from app.behavior_tags.runtime import LocalBehaviorRuntime

router = APIRouter(prefix="/api/v1/behavior-tags", tags=["local-behavior-tags"])
ReadPrincipal = Annotated[DataLoopPrincipal, Depends(require_hot_news_permission(HotNewsPermission.READ))]


def runtime(request: Request) -> LocalBehaviorRuntime:
    value = getattr(request.app.state, "behavior_tags_runtime", None)
    if value is None:
        raise HTTPException(503, "本地行为标签未启用。")
    return value


class LabelSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window_start: AwareDatetime
    window_end: AwareDatetime
    include_topic_ids: tuple[str, ...] = Field(default=(), max_length=10)
    exclude_topic_ids: tuple[str, ...] = Field(default=(), max_length=10)
    behavior_label_ids: tuple[str, ...] = Field(default=(), max_length=32)
    match: Literal["any", "all"] = "any"
    dataset_version: str | None = Field(default=None, min_length=1, max_length=160)
    scenario_id: str | None = Field(default=None, min_length=1, max_length=160)
    limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def bounded_window(self) -> Self:
        if not timedelta(0) < self.window_end - self.window_start <= timedelta(days=60):
            raise ValueError("search window must be within 60 days")
        if set(self.include_topic_ids) & set(self.exclude_topic_ids):
            raise ValueError("include and exclude topics must be disjoint")
        if any(len(value) > 160 for value in (*self.include_topic_ids, *self.exclude_topic_ids, *self.behavior_label_ids)):
            raise ValueError("label identifier exceeds bound")
        return self


@router.get("/topics")
def topics(principal: ReadPrincipal, response: Response,
           index: LocalBehaviorRuntime = Depends(runtime)):
    response.headers["Cache-Control"] = "no-store"
    return {"catalog": index.policy.catalog.model_dump(mode="json"),
        "policy_version": index.policy.policy_version, "package_sha256": index.package_sha256,
        "source_mode": "synthetic"}


@router.post("/search")
def search(body: LabelSearch, principal: ReadPrincipal, response: Response,
           index: LocalBehaviorRuntime = Depends(runtime)):
    response.headers["Cache-Control"] = "no-store"
    try:
        rows = index.search(tenant_id=str(principal.tenant_id), **body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, "标签、场景或时间窗口与本地数据包不匹配。") from exc
    return {"source_mode": "synthetic", "package_sha256": index.package_sha256,
        "results": rows, "dispatch_enabled": False}
