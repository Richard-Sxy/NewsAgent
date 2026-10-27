"""知识库接口。接收爬虫提交的新闻文档、生成并入库QA、查询文档索引状态。会把请求数据转换成 KnowledgeDocument。"""

from __future__ import annotations

from secrets import compare_digest
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, Field
from pydantic import SecretStr

from app.modules.knowledge.document import KnowledgeDocument
from app.modules.knowledge.postgres_store import PostgresKnowledgeStore
from app.modules.knowledge.tier_lifecycle_query import LifecycleQueryTimeout, LifecycleQueryUnavailable


router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])


class KnowledgeDocumentPayload(BaseModel):
    document_id: str = Field(min_length=1, max_length=160)
    news_id: str = Field(min_length=1, max_length=160)
    content_version: int = Field(ge=1, le=9_223_372_036_854_775_807)
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=200000)
    publish_time: AwareDatetime
    source_url: str = Field(default="", max_length=2000)
    media_type: str = Field(default="article", min_length=1, max_length=32)
    text_source: str = Field(default="crawler", min_length=1, max_length=64)
    metadata: dict[str, str] = Field(default_factory=dict)

    def as_document(self) -> KnowledgeDocument:
        metadata = {
            **self.metadata,
            "news_id": self.news_id,
            "content_version": str(self.content_version),
            "publish_time": self.publish_time.isoformat(),
        }
        return KnowledgeDocument(
            document_id=self.document_id,
            title=self.title,
            text=self.text,
            media_type=self.media_type,
            text_source=self.text_source,
            source_url=self.source_url,
            metadata=metadata,
        )


class KnowledgeIngestRequest(BaseModel):
    documents: tuple[KnowledgeDocumentPayload, ...] = Field(min_length=1, max_length=20)


def _authorized_store(
    request: Request,
    authorization: str | None,
) -> PostgresKnowledgeStore:
    settings = request.app.state.settings
    configured = settings.knowledge_ingest_token
    secret = configured.get_secret_value() if isinstance(configured, SecretStr) else ""
    parts = authorization.split() if authorization else []
    if not secret:
        raise HTTPException(status_code=503, detail="knowledge ingest credential is not configured")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not compare_digest(parts[1], secret):
        raise HTTPException(status_code=401, detail="invalid knowledge ingest credential")
    store = getattr(request.app.state, "knowledge_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Python knowledge runtime is unavailable")
    return store


@router.post("/documents")
async def ingest_documents(
    payload: KnowledgeIngestRequest,
    request: Request,
    x_tenant_id: Annotated[UUID, Header(alias="X-Tenant-ID")],
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    store = _authorized_store(request, authorization)
    report = await store.upsert_documents(
        tenant_id=str(x_tenant_id),
        documents=tuple(item.as_document() for item in payload.documents),
    )
    return {
        "outcomes": [
            {
                "document_id": item.document_id,
                "status": item.status.value,
                "collection_id": item.collection_id,
                "detail": item.detail,
            }
            for item in report.outcomes
        ]
    }


@router.post("/qa")
async def generate_qa(
    payload: KnowledgeDocumentPayload,
    request: Request,
    x_tenant_id: Annotated[UUID, Header(alias="X-Tenant-ID")],
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    _authorized_store(request, authorization)
    if getattr(request.app.state, "knowledge_lifecycle", None) is not None:
        raise HTTPException(status_code=409, detail="news lifecycle does not yet support QA child projections")
    service = getattr(request.app.state, "qa_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Python QA runtime is unavailable")
    try:
        report = await service.generate_and_index(
            tenant_id=str(x_tenant_id), source=payload.as_document()
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="QA evidence validation failed") from exc
    return {
        "outcomes": [
            {
                "document_id": item.document_id,
                "status": item.status.value,
                "collection_id": item.collection_id,
                "detail": item.detail,
            }
            for item in report.outcomes
        ]
    }


@router.get("/documents/{document_id}/status")
async def document_status(
    document_id: str,
    request: Request,
    x_tenant_id: Annotated[UUID, Header(alias="X-Tenant-ID")],
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    store = _authorized_store(request, authorization)
    result = await store.get_document_status(
        tenant_id=str(x_tenant_id), document_id=document_id
    )
    if result is None:
        raise HTTPException(status_code=404, detail="document is not indexed")
    return {"status": "ready", **result}


class RetractionPayload(BaseModel):
    source_revision: int = Field(ge=1, le=9_223_372_036_854_775_807)


class LifecycleSearchPayload(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=10, ge=1, le=1000)
    retrieval_intent: Literal["auto", "general", "recent", "background"] = "auto"
    retrieval_topic: str | None = Field(default=None, min_length=1, max_length=128)


class DuplicateRetirementPayload(BaseModel):
    source_revision: int = Field(ge=1, le=9_223_372_036_854_775_807)
    source_job_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    replacement_news_id: str = Field(min_length=1, max_length=160)
    replacement_revision: int = Field(ge=1, le=9_223_372_036_854_775_807)
    replacement_job_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class DuplicateRestorePayload(BaseModel):
    retirement_job_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class SemanticRetirementPayload(BaseModel):
    decision_id: str = Field(pattern=r"^[0-9a-f]{64}$")


def _semantics(request, authorization):
    service = _lifecycle(request, authorization).service.semantics
    if service is None:
        raise HTTPException(status_code=503, detail="news semantics is not enabled")
    return service


@router.get("/news/{news_id}/semantics")
async def news_semantics(news_id: str, request: Request,
    x_tenant_id: Annotated[UUID, Header(alias="X-Tenant-ID")],
    authorization: Annotated[str | None, Header()] = None):
    result = await _semantics(request, authorization).status(str(x_tenant_id), news_id)
    if result is None:
        raise HTTPException(status_code=404, detail="news not found")
    return result


@router.post("/news/{news_id}/semantics/retry", status_code=202)
async def retry_news_semantics(news_id: str, request: Request,
    x_tenant_id: Annotated[UUID, Header(alias="X-Tenant-ID")],
    authorization: Annotated[str | None, Header()] = None):
    service = _semantics(request, authorization)
    row = await service.lifecycle.ledger.get_news(tenant_id=str(x_tenant_id), news_id=news_id)
    if not row or row["status"] != "active":
        raise HTTPException(status_code=409, detail="only active news can be reprocessed")
    await service.store.retry(row)
    return {"status": "accepted", "news_id": news_id}


@router.post("/semantic-decisions/retire", status_code=202)
async def retire_semantic_news(payload: SemanticRetirementPayload, request: Request,
    x_tenant_id: Annotated[UUID, Header(alias="X-Tenant-ID")],
    authorization: Annotated[str | None, Header()] = None):
    try:
        job = await _semantics(request, authorization).retire(
            tenant_id=str(x_tenant_id), decision_id=payload.decision_id)
        return _public_job(job)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _lifecycle(request, authorization):
    _authorized_store(request, authorization)
    runtime = getattr(request.app.state, "knowledge_lifecycle", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="knowledge lifecycle is not enabled")
    return runtime


def _public_job(job):
    # Never expose canonical text, vectors or operational config through status.
    return {key: job[key] for key in ("job_id", "news_id", "source_revision", "generation",
        "kind", "target_tier", "status", "phase", "attempts", "last_error", "cleanup_dirty")}


@router.post("/news/{news_id}/retract", status_code=202)
async def retract_news(news_id: str, payload: RetractionPayload, request: Request,
    x_tenant_id: Annotated[UUID, Header(alias="X-Tenant-ID")],
    authorization: Annotated[str | None, Header()] = None):
    service = _lifecycle(request, authorization).service
    try:
        return _public_job(await service.retract(tenant_id=str(x_tenant_id), news_id=news_id,
                                                source_revision=payload.source_revision))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/news/{news_id}/reheat", status_code=202)
async def reheat_news(news_id: str, request: Request,
    x_tenant_id: Annotated[UUID, Header(alias="X-Tenant-ID")],
    authorization: Annotated[str | None, Header()] = None):
    service = _lifecycle(request, authorization).service
    try:
        job = await service.reheat(tenant_id=str(x_tenant_id), news_id=news_id)
        return _public_job(job) if job else {"status": "unchanged"}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/news/{news_id}/retire-duplicate", status_code=202)
async def retire_duplicate_news(news_id: str, payload: DuplicateRetirementPayload, request: Request,
    x_tenant_id: Annotated[UUID, Header(alias="X-Tenant-ID")],
    authorization: Annotated[str | None, Header()] = None):
    service = _lifecycle(request, authorization).service
    try:
        return _public_job(await service.retire_duplicate(
            tenant_id=str(x_tenant_id), news_id=news_id, **payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/news/{news_id}/restore-duplicate", status_code=202)
async def restore_duplicate_news(news_id: str, payload: DuplicateRestorePayload, request: Request,
    x_tenant_id: Annotated[UUID, Header(alias="X-Tenant-ID")],
    authorization: Annotated[str | None, Header()] = None):
    service = _lifecycle(request, authorization).service
    try:
        return _public_job(await service.restore_duplicate(
            tenant_id=str(x_tenant_id), news_id=news_id, **payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/index-jobs/{job_id}")
async def index_job_status(job_id: str, request: Request,
    x_tenant_id: Annotated[UUID, Header(alias="X-Tenant-ID")],
    authorization: Annotated[str | None, Header()] = None):
    ledger = _lifecycle(request, authorization).service.ledger
    job = await ledger.get_job(tenant_id=str(x_tenant_id), job_id=job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="index job not found")
    return _public_job(job)


@router.post("/search")
async def lifecycle_search(payload: LifecycleSearchPayload, request: Request,
    x_tenant_id: Annotated[UUID, Header(alias="X-Tenant-ID")],
    authorization: Annotated[str | None, Header()] = None):
    runtime = _lifecycle(request, authorization)
    try:
        hints = {}
        if payload.retrieval_intent != "auto" or payload.retrieval_topic is not None:
            hints = {"retrieval_intent": payload.retrieval_intent, "retrieval_topic": payload.retrieval_topic}
        result = await runtime.query.search(query_text=payload.query, tenant_id=str(x_tenant_id),
                                           limit=payload.limit, **hints)
    except LifecycleQueryTimeout as exc:
        raise HTTPException(status_code=504, detail={"code": exc.code, "trace": exc.trace}) from None
    except LifecycleQueryUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "trace": exc.trace}) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"items": [{"news_id": hit.news_id, "chunk_id": hit.chunk_id,
        "content_version": hit.content_version, "tier": hit.tier.value,
        "title": hit.title, "excerpt": hit.excerpt, "source_url": hit.source_url}
        for hit in result["hits"]], "trace": result["trace"], "evidence_is_untrusted": True}


@router.get("/lifecycle/readiness")
async def lifecycle_readiness(request: Request,
    x_tenant_id: Annotated[UUID, Header(alias="X-Tenant-ID")],
    authorization: Annotated[str | None, Header()] = None):
    runtime = _lifecycle(request, authorization)
    try:
        tiers = await runtime.validate()
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "knowledge_lifecycle_not_ready",
            "error_type": type(exc).__name__}) from None
    return {"status": "ready", "scope": "database_and_vector_contract_only",
        "tiers": {tier: {key: value for key, value in details.items() if key != "varchar_limits"}
                  for tier, details in tiers.items()}}


@router.post("/index-jobs/{job_id}/retry", status_code=202)
async def retry_index_job(job_id: str, request: Request,
    x_tenant_id: Annotated[UUID, Header(alias="X-Tenant-ID")],
    authorization: Annotated[str | None, Header()] = None):
    ledger = _lifecycle(request, authorization).service.ledger
    try:
        return _public_job(await ledger.retry_job(tenant_id=str(x_tenant_id), job_id=job_id))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
