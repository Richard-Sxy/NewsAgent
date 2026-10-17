"""Bind enterprise interest aggregates to an authorized hot-news observation."""

import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from app.audience.contracts import (
    AudienceProfilePort, AudienceProviderError, AudienceQuery, AudienceSelection,
    AudienceSnapshot, audience_query_fingerprint,
)
from app.audience.taxonomy import TopicCatalog
from app.observability.telemetry import operation
from app.schemas.hot_news_memory import HotNewsAnalysisMemory


class AnalysisReader(Protocol):
    async def get_analysis_memory(self, *, tenant_id: str, run_id: UUID,
                                  news_id: str) -> HotNewsAnalysisMemory | None: ...


class AudienceSelectionError(ValueError):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


class AudienceTargetNotFound(LookupError):
    pass


class AudienceSnapshotError(ValueError):
    pass


class AudienceRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["consider", "observe"]
    reason: str


class AudiencePreview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    preview_only: Literal[True] = True
    manual_review_required: Literal[True] = True
    dispatch_enabled: Literal[False] = False
    run_id: UUID
    news_id: str
    selection: AudienceSelection
    snapshot: AudienceSnapshot
    recommendation: AudienceRecommendation
    fingerprint: str
    limitations: tuple[str, ...]


class AudiencePreviewService:
    def __init__(self, *, catalog: TopicCatalog, client: AudienceProfilePort,
                 run_store: AnalysisReader, source_mode: Literal["enterprise", "synthetic"],
                 timeout_seconds: float = 5, max_snapshot_age_seconds: int = 900,
                 clock=None):
        if not 0 < timeout_seconds <= 30 or not 1 <= max_snapshot_age_seconds <= 3600:
            raise ValueError("invalid audience request or freshness budget")
        if source_mode not in {"enterprise", "synthetic"}:
            raise ValueError("unknown audience source mode")
        self.catalog = catalog
        self.client = client
        self.run_store = run_store
        self.source_mode = source_mode
        self.timeout_seconds = timeout_seconds
        self.max_snapshot_age_seconds = max_snapshot_age_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def topics(self) -> dict:
        return {**self.catalog.model_dump(mode="json"), "source_mode": self.source_mode}

    async def close(self):
        await self.client.close()

    async def preview(self, *, tenant_id: UUID, run_id: UUID, news_id: str,
                      selection: AudienceSelection) -> AudiencePreview:
        # Revalidation also guards internal callers using model_construct/copy.
        selection = AudienceSelection.model_validate(selection.model_dump(mode="json"))
        if selection.taxonomy_version != self.catalog.taxonomy_version:
            raise AudienceSelectionError("主题目录版本已变化，请重新选择标签。", 409)
        try:
            self.catalog.validate_ids(selection.include_topic_ids)
            self.catalog.validate_ids(selection.exclude_topic_ids)
        except ValueError as exc:
            raise AudienceSelectionError("包含未知或停用标签，请刷新目录。") from exc
        memory = await self.run_store.get_analysis_memory(
            tenant_id=str(tenant_id), run_id=run_id, news_id=news_id)
        if memory is None:
            raise AudienceTargetNotFound("当前租户没有这篇新闻的已完成分析。")
        if (str(memory.tenant_id) != str(tenant_id) or memory.run_id != run_id
                or memory.news_id != news_id or memory.analysis_input.news_id != news_id
                or memory.analysis_report.news_id != news_id):
            raise AudienceSnapshotError("热点分析身份不一致。")
        try:
            query = AudienceQuery(tenant_id=tenant_id, run_id=run_id, news_id=news_id,
                window_start=memory.analysis_input.window_start,
                window_end=memory.analysis_input.window_end, selection=selection)
        except ValidationError as exc:
            raise AudienceSnapshotError("热点分析窗口不符合受众查询合同。") from exc
        if query.window_end > self.clock() + timedelta(seconds=30):
            raise AudienceSnapshotError("热点分析窗口尚未完成，未查询受众。")
        with operation("audience.preview"):
            try:
                raw = await asyncio.wait_for(self.client.estimate(query), self.timeout_seconds)
            except TimeoutError as exc:
                raise AudienceProviderError("timeout", retryable=True) from exc
            try:
                snapshot = AudienceSnapshot.model_validate(raw.model_dump(mode="json"))
            except (ValidationError, AttributeError) as exc:
                raise AudienceSnapshotError("受众快照不符合聚合合同。") from exc
            self._validate_snapshot(query, snapshot)
        eligible = snapshot.eligible_users
        recommendation = AudienceRecommendation(
            action="consider" if eligible > 0 else "observe",
            reason=("存在符合兴趣与企业通知资格的去重受众，可进入人工推送评估；未创建投递任务。"
                    if eligible > 0 else "当前组合没有符合通知资格的受众，建议继续观察或调整主题；未创建投递任务。"))
        content = query.model_dump_json() + "\x1f" + snapshot.model_dump_json() + "\x1faudience-preview-v1"
        limitations = [
            "受众按标签组合统一去重，不能相加各主题人数；与新闻浏览UV口径不同。",
            "这是所选主题在报告窗口对应的画像预估，不代表该新闻全站潜在受众或当前实时可送达人数。",
            "人数不是送达、展示或点击预测；实际推送仍须企业平台重新校验同意、频控、时效与人工审批。",
            "预览不保存审批或投递状态，fingerprint不承诺持久幂等重放。",
        ]
        if self.source_mode == "synthetic":
            limitations.insert(0, "当前为合成标签人群演示，不是腾讯新闻真实用户或推荐效果。")
        return AudiencePreview(run_id=run_id, news_id=news_id, selection=selection,
            snapshot=snapshot, recommendation=recommendation,
            fingerprint=sha256(content.encode()).hexdigest(), limitations=tuple(limitations))

    def _validate_snapshot(self, query: AudienceQuery, snapshot: AudienceSnapshot):
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("audience clock must be timezone aware")
        if (snapshot.tenant_id != query.tenant_id or snapshot.run_id != query.run_id
                or snapshot.news_id != query.news_id
                or snapshot.taxonomy_version != query.selection.taxonomy_version
                or snapshot.query_fingerprint != audience_query_fingerprint(query)
                or (snapshot.window_start, snapshot.window_end) != (query.window_start, query.window_end)
                or snapshot.source_mode != self.source_mode):
            raise AudienceSnapshotError("受众快照与本次租户、新闻或查询不一致。")
        budget = timedelta(seconds=self.max_snapshot_age_seconds)
        if (not snapshot.is_complete or snapshot.watermark < query.window_end
                or snapshot.watermark > min(now, snapshot.generated_at) + timedelta(seconds=30)
                or query.window_end > snapshot.generated_at + timedelta(seconds=30)
                or snapshot.generated_at > now + timedelta(seconds=30)
                or snapshot.generated_at < now - budget or snapshot.expires_at <= now
                or snapshot.expires_at - snapshot.generated_at > budget):
            raise AudienceSnapshotError("受众快照已失效或数据水位不完整。")
