"""企业新闻内容服务 RPC 的批量读取契约。"""

from typing import Literal, Protocol

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
from app.analytics.entities import ContentType
from app.analytics.news_content import NewsContent, NewsContentRepository


class NewsContentBatchRequest(BaseModel):
    """按统一 news_id 批量读取分析所需内容，避免逐条 RPC。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    news_ids: tuple[NonBlank128, ...] = Field(min_length=1, max_length=500)
    include_body: bool = True
    body_max_chars: int = Field(default=10_000, ge=0, le=100_000)


class NewsContentRow(BaseModel):
    """内容服务的规范化响应；受限或已删除内容不应进入分析层。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    news_id: NonBlank128
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(default="", max_length=2_000)
    body: str = Field(default="", max_length=100_000)
    content_type: Literal["article", "video"]
    publish_time: AwareDatetime
    canonical_url: NonBlank256
    content_version: NonBlank128
    status: Literal["published", "removed", "restricted"]


class NewsContentBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[NewsContentRow, ...]
    missing_news_ids: tuple[NonBlank128, ...] = ()
    meta: RpcResponseMeta


class NewsContentRpc(Protocol):
    """企业内容中心的 RPC Port。"""

    async def batch_get_news(
        self,
        *,
        context: RpcCallContext,
        request: NewsContentBatchRequest,
    ) -> NewsContentBatchResponse: ...


class RpcNewsContentRepository(NewsContentRepository):
    """通过企业内容 RPC 批量读取并映射可用于分析的新闻正文。"""

    def __init__(
        self,
        client: NewsContentRpc,
        *,
        context_provider: RpcCallContextProvider | None = None,
        timeout_ms: int = 15_000,
        batch_size: int = 200,
        body_max_chars: int = 10_000,
    ) -> None:
        if not 1 <= batch_size <= 500:
            raise ValueError("batch_size must be between 1 and 500")
        if not 0 <= body_max_chars <= 100_000:
            raise ValueError("body_max_chars must be between 0 and 100000")
        self._client = client
        self._context_provider = (
            context_provider or DefaultRpcCallContextProvider()
        )
        self._timeout_ms = timeout_ms
        self._batch_size = batch_size
        self._body_max_chars = body_max_chars

    async def batch_get_by_news_ids(
        self,
        *,
        tenant_id: str,
        news_ids: tuple[str, ...],
    ) -> dict[str, NewsContent]:
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        unique_ids = tuple(dict.fromkeys(news_ids))
        if any(not news_id.strip() for news_id in unique_ids):
            raise ValueError("news_ids cannot contain empty values")
        if not unique_ids:
            return {}

        context = self._context_provider.create(
            tenant_id=tenant_id,
            operation="news-content-batch-get",
            timeout_ms=self._timeout_ms,
        )
        budget = RpcDeadlineBudget(context)
        contents: dict[str, NewsContent] = {}

        for offset in range(0, len(unique_ids), self._batch_size):
            requested_chunk = unique_ids[offset : offset + self._batch_size]
            response = await self._client.batch_get_news(
                context=budget.next_context(),
                request=NewsContentBatchRequest(
                    news_ids=requested_chunk,
                    include_body=True,
                    body_max_chars=self._body_max_chars,
                ),
            )
            requested_set = set(requested_chunk)
            if not set(response.missing_news_ids).issubset(requested_set):
                raise EnterpriseRpcResponseError(
                    "content RPC returned an unexpected missing news_id",
                    request_id=response.meta.request_id,
                )

            returned_ids: set[str] = set()
            for row in response.items:
                if row.news_id not in requested_set:
                    raise EnterpriseRpcResponseError(
                        "content RPC returned an unrequested news_id",
                        request_id=response.meta.request_id,
                    )
                if row.news_id in contents:
                    raise EnterpriseRpcResponseError(
                        "content RPC returned a duplicate news_id",
                        request_id=response.meta.request_id,
                    )
                if row.news_id in returned_ids:
                    raise EnterpriseRpcResponseError(
                        "content RPC returned a duplicate news_id in one batch",
                        request_id=response.meta.request_id,
                    )
                returned_ids.add(row.news_id)
                if row.status != "published":
                    continue
                content = NewsContent(
                    news_id=row.news_id,
                    title=row.title,
                    summary=row.summary,
                    body=row.body,
                    content_type=ContentType(row.content_type),
                    publish_time=row.publish_time,
                    source_url=row.canonical_url,
                )
                try:
                    content.validate()
                except ValueError as exc:
                    raise EnterpriseRpcResponseError(
                        f"invalid enterprise content row: {exc}",
                        request_id=response.meta.request_id,
                    ) from exc
                contents[row.news_id] = content

            missing_ids = set(response.missing_news_ids)
            if returned_ids & missing_ids:
                raise EnterpriseRpcResponseError(
                    "content RPC marked a returned news_id as missing",
                    request_id=response.meta.request_id,
                )
            if returned_ids | missing_ids != requested_set:
                raise EnterpriseRpcResponseError(
                    "content RPC response is incomplete for requested news_ids",
                    request_id=response.meta.request_id,
                )

        return contents
