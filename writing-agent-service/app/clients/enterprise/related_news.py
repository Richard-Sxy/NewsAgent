"""企业检索服务的批量关联新闻召回契约。"""

from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.clients.enterprise.common import (
    DefaultRpcCallContextProvider,
    EnterpriseRpcResponseError,
    NonBlank128,
    NonBlank256,
    RpcCallContext,
    RpcCallContextProvider,
    RpcDeadlineBudget,
    RpcResponseMeta,
)
from app.clients.knowledge_base import (
    KnowledgeSearchClient,
    RelatedNews,
    RelatedNewsSearchQuery,
)


class RelatedNewsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: NonBlank128
    source_news_id: NonBlank128
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(default="", max_length=2_000)
    exclude_news_ids: tuple[NonBlank128, ...] = ()
    candidate_limit: int = Field(default=20, ge=1, le=200)


class RelatedNewsBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    queries: tuple[RelatedNewsQuery, ...] = Field(min_length=1, max_length=100)
    retrieval_policy_version: NonBlank128


class RelatedNewsCandidate(BaseModel):
    """召回阶段输出；最终分数仍由本项目规则重排器计算。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    news_id: NonBlank128 | None = None
    document_id: NonBlank256
    title: str = Field(default="", max_length=500)
    excerpt: str = Field(default="", max_length=2_000)
    source_url: NonBlank256 | None = None
    publish_time: AwareDatetime | None = None
    raw_score: float | None = None
    normalized_score: float | None = Field(default=None, ge=0, le=1)
    retrieval_channel: NonBlank128
    metadata_version: NonBlank128 | None = None


class RelatedNewsQueryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: NonBlank128
    source_news_id: NonBlank128
    candidates: tuple[RelatedNewsCandidate, ...]


class RelatedNewsBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    results: tuple[RelatedNewsQueryResult, ...]
    retrieval_policy_version: NonBlank128
    index_version: NonBlank128
    meta: RpcResponseMeta


class RelatedNewsRetrievalRpc(Protocol):
    """企业向量/混合检索服务的 RPC Port。"""

    async def batch_search_related_news(
        self,
        *,
        context: RpcCallContext,
        request: RelatedNewsBatchRequest,
    ) -> RelatedNewsBatchResponse: ...


class RpcRelatedNewsSearchClient(KnowledgeSearchClient):
    """将企业批量召回结果转换为本地规则重排器使用的候选。"""

    def __init__(
        self,
        client: RelatedNewsRetrievalRpc,
        *,
        retrieval_policy_version: str,
        context_provider: RpcCallContextProvider | None = None,
        timeout_ms: int = 20_000,
        batch_size: int = 50,
    ) -> None:
        if not retrieval_policy_version.strip():
            raise ValueError("retrieval_policy_version cannot be empty")
        if not 1 <= batch_size <= 100:
            raise ValueError("batch_size must be between 1 and 100")
        self._client = client
        self._retrieval_policy_version = retrieval_policy_version.strip()
        self._context_provider = (
            context_provider or DefaultRpcCallContextProvider()
        )
        self._timeout_ms = timeout_ms
        self._batch_size = batch_size

    async def batch_search_related_news(
        self,
        queries: tuple[RelatedNewsSearchQuery, ...],
        *,
        tenant_id: str,
    ) -> dict[str, list[RelatedNews]]:
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        for query in queries:
            query.validate()
        query_ids = [query.query_id for query in queries]
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("queries cannot contain duplicate query_id values")
        if not queries:
            return {}

        context = self._context_provider.create(
            tenant_id=tenant_id,
            operation="related-news-batch-search",
            timeout_ms=self._timeout_ms,
        )
        budget = RpcDeadlineBudget(context)
        index_version: str | None = None
        results: dict[str, list[RelatedNews]] = {}

        for offset in range(0, len(queries), self._batch_size):
            chunk = queries[offset : offset + self._batch_size]
            by_query_id = {query.query_id: query for query in chunk}
            response = await self._client.batch_search_related_news(
                context=budget.next_context(),
                request=RelatedNewsBatchRequest(
                    queries=tuple(
                        RelatedNewsQuery(
                            query_id=query.query_id,
                            source_news_id=query.source_news_id,
                            title=query.title,
                            summary=query.summary,
                            exclude_news_ids=tuple(
                                dict.fromkeys(
                                    (*query.exclude_news_ids, query.source_news_id)
                                )
                            ),
                            candidate_limit=query.candidate_limit,
                        )
                        for query in chunk
                    ),
                    retrieval_policy_version=self._retrieval_policy_version,
                ),
            )
            if (
                response.retrieval_policy_version
                != self._retrieval_policy_version
            ):
                raise EnterpriseRpcResponseError(
                    "retrieval RPC returned an unexpected policy version",
                    request_id=response.meta.request_id,
                )
            if index_version is None:
                index_version = response.index_version
            elif response.index_version != index_version:
                raise EnterpriseRpcResponseError(
                    "retrieval index version changed during one batch",
                    request_id=response.meta.request_id,
                )

            response_ids = [item.query_id for item in response.results]
            if set(response_ids) != set(by_query_id):
                raise EnterpriseRpcResponseError(
                    "retrieval RPC response does not match requested queries",
                    request_id=response.meta.request_id,
                )
            if len(response_ids) != len(set(response_ids)):
                raise EnterpriseRpcResponseError(
                    "retrieval RPC returned duplicate query results",
                    request_id=response.meta.request_id,
                )

            for query_result in response.results:
                query = by_query_id[query_result.query_id]
                if query_result.source_news_id != query.source_news_id:
                    raise EnterpriseRpcResponseError(
                        "retrieval response source_news_id mismatch",
                        request_id=response.meta.request_id,
                    )
                excluded = set(query.exclude_news_ids)
                excluded.add(query.source_news_id)
                seen_documents: set[str] = set()
                mapped: list[RelatedNews] = []
                for candidate in query_result.candidates:
                    if candidate.document_id in seen_documents:
                        continue
                    seen_documents.add(candidate.document_id)
                    if candidate.news_id in excluded:
                        continue
                    score = candidate.normalized_score
                    if (
                        score is None
                        and candidate.raw_score is not None
                        and 0 <= candidate.raw_score <= 1
                    ):
                        score = candidate.raw_score
                    mapped.append(
                        RelatedNews(
                            collection_id=candidate.document_id,
                            news_id=candidate.news_id,
                            title=candidate.title or "未命名关联报道",
                            text=candidate.excerpt,
                            source_url=candidate.source_url,
                            publish_time=(
                                candidate.publish_time.isoformat()
                                if candidate.publish_time is not None
                                else None
                            ),
                            score=score,
                        )
                    )
                    if len(mapped) >= query.candidate_limit:
                        break
                results[query.query_id] = mapped

        return results
