"""知识库接口。接收爬虫提交的新闻文档、生成并入库QA、查询文档索引状态。会把请求数据转换成 KnowledgeDocument。"""

from __future__ import annotations

from secrets import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, Field
from pydantic import SecretStr

from app.knowledge.document import KnowledgeDocument
from app.knowledge.postgres_store import PostgresKnowledgeStore


router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])


class KnowledgeDocumentPayload(BaseModel):
    document_id: str = Field(min_length=1, max_length=160)
    news_id: str = Field(min_length=1, max_length=160)
    content_version: int = Field(ge=1)
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
    return {**result, "status": "ready"}
