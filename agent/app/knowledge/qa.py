"""生成并入库问答知识。NativeQAGenerationService 调用结构化模型生成问题、答案和原文引用，检查引用是否存在、问题是否重复，再转成QA文档入库。"""

from __future__ import annotations

from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field

from app.knowledge.document import IngestReport, KnowledgeDocument
from app.knowledge.postgres_store import PostgresKnowledgeStore
from app.model_runtime.agent_client import NativeStructuredAgentClient, model_request_context


class QAPair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=4, max_length=500)
    answer: str = Field(min_length=1, max_length=2000)
    evidence_quote: str = Field(min_length=1, max_length=1000)


class QAGenerationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pairs: tuple[QAPair, ...] = Field(min_length=1, max_length=12)


class NativeQAGenerationService:
    def __init__(
        self,
        *,
        agent: NativeStructuredAgentClient,
        store: PostgresKnowledgeStore,
    ) -> None:
        self._agent = agent
        self._store = store

    async def generate_and_index(
        self,
        *,
        tenant_id: str,
        source: KnowledgeDocument,
    ) -> IngestReport:
        source.validate()
        with model_request_context(
            tenant_id=tenant_id,
            trace_id=f"knowledge-qa-{sha256(source.document_id.encode('utf-8')).hexdigest()}",
        ):
            result = await self._agent.run_structured(
                app_id="python:qa",
                payload={
                    "document_id": source.document_id,
                    "title": source.title,
                    "text": source.text,
                    "source_url": source.source_url,
                },
                output_type=QAGenerationOutput,
                mode="qa_generation",
            )
        questions: set[str] = set()
        documents: list[KnowledgeDocument] = []
        for index, pair in enumerate(result.value.pairs):
            if pair.evidence_quote not in source.text:
                raise ValueError("QA evidence quote is absent from source document")
            if pair.question in questions:
                raise ValueError("QA contains duplicate questions")
            questions.add(pair.question)
            document_id = sha256(
                f"{tenant_id}\x1f{source.document_id}\x1fqa\x1f{index}".encode("utf-8")
            ).hexdigest()
            documents.append(
                KnowledgeDocument(
                    document_id=f"qa-{document_id}",
                    title=pair.question,
                    text=(
                        f"问题：{pair.question}\n回答：{pair.answer}\n"
                        f"来源原文：{pair.evidence_quote}"
                    ),
                    media_type="qa",
                    text_source="python-qa-generation",
                    source_url=source.source_url,
                    metadata={
                        **source.metadata,
                        "qa_source_document_id": source.document_id,
                        "qa_prompt_scene": "qa_generation",
                    },
                )
            )
        return await self._store.upsert_documents(
            tenant_id=tenant_id, documents=tuple(documents)
        )
