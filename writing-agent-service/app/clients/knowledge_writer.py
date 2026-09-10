"""FastGPT 知识库写入适配器。

实现 ``KnowledgeBaseWriter`` Port，把 ``KnowledgeDocument`` 写入 FastGPT 数据集。
payload 结构对齐仓库中已验证可用的 ``tencent-news-crawler/service/fastgpt_client.py``，
但补了两件爬虫侧没有的事：

1. **幂等**：先按 ``document_id`` 查询同名 Collection，存在则按策略跳过，
   避免 Temporal 重试或重复跑批产生重复文档。
2. **元数据**：写入 ``media_type`` / ``text_source`` 等内容类型字段，
   让检索侧能按类型过滤，并对降级文档降权。

已知限制（需要企业/环境确认后才能补齐）：

- FastGPT 的 ``create/text`` 是"新建"语义。当前 ``existing_strategy`` 只实现
  ``skip``；``replace``（删除后重建）需要确认 ``collection/delete`` 的 HTTP 方法与
  权限模型，暂未实现，重复写入不会刷新已存在的文档内容。
- 该实现面向 FastGPT；若生产入库目标是企业检索平台索引，应另写一个
  ``KnowledgeBaseWriter`` 实现，上层文本化逻辑完全复用。
"""

import asyncio
from typing import Any, Mapping, Protocol

import httpx

from app.domain.errors import (
    FastGPTAuthenticationError,
    FastGPTError,
    FastGPTNetworkError,
    FastGPTRateLimitError,
    FastGPTRequestError,
    FastGPTResponseError,
    FastGPTServerError,
    FastGPTTimeoutError,
)
from app.knowledge.document import (
    IngestOutcome,
    IngestReport,
    IngestStatus,
    KnowledgeDocument,
)


class KnowledgeWriterConfig(Protocol):
    fastgpt_base_url: str
    fastgpt_api_key: str
    fastgpt_dataset_id: str | None


class FastGPTKnowledgeWriter:
    """把知识文档写入 FastGPT 数据集 Collection。"""

    _CREATE_PATH = "/api/core/dataset/collection/create/text"
    _LIST_PATH = "/api/core/dataset/collection/list"

    def __init__(
        self,
        settings: KnowledgeWriterConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 60,
        max_concurrency: int = 4,
        existing_strategy: str = "skip",
        training_type: str = "chunk",
    ) -> None:
        if not settings.fastgpt_dataset_id:
            raise ValueError("FASTGPT_DATASET_ID is required for knowledge writing")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be greater than 0")
        if existing_strategy not in {"skip"}:
            raise ValueError(
                "existing_strategy currently supports only 'skip'"
            )
        self.base_url = settings.fastgpt_base_url.rstrip("/")
        self.api_key = settings.fastgpt_api_key
        self.dataset_id = settings.fastgpt_dataset_id
        self.http_client = http_client or httpx.AsyncClient()
        self._owns_client = http_client is None
        self.timeout = httpx.Timeout(timeout_seconds, connect=10)
        self.max_concurrency = max_concurrency
        self.existing_strategy = existing_strategy
        self.training_type = training_type

    async def close(self) -> None:
        if self._owns_client:
            await self.http_client.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def upsert_documents(
        self,
        *,
        tenant_id: str,
        documents: tuple[KnowledgeDocument, ...],
    ) -> IngestReport:
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        if not documents:
            return IngestReport()

        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def write_one(document: KnowledgeDocument) -> IngestOutcome:
            document.validate()
            async with semaphore:
                return await self._write_one(document)

        outcomes = await asyncio.gather(
            *(write_one(document) for document in documents)
        )
        return IngestReport(outcomes=tuple(outcomes))

    async def _write_one(self, document: KnowledgeDocument) -> IngestOutcome:
        existing_id = await self._find_collection_id(document.document_id)
        if existing_id is not None and self.existing_strategy == "skip":
            return IngestOutcome(
                document_id=document.document_id,
                status=IngestStatus.SKIPPED,
                collection_id=existing_id,
                detail="collection already exists; skipped by strategy",
            )
        try:
            collection_id = await self._create_text_collection(document)
        except FastGPTError as exc:
            return IngestOutcome(
                document_id=document.document_id,
                status=IngestStatus.FAILED,
                detail=f"{type(exc).__name__}: {exc}"[:500],
            )
        return IngestOutcome(
            document_id=document.document_id,
            status=IngestStatus.CREATED,
            collection_id=collection_id,
        )

    async def _find_collection_id(self, document_id: str) -> str | None:
        """按名称精确匹配已存在的 Collection。

        列表接口失败时必须抛错而不是返回 ``None``：把"查不到"误判成"不存在"
        会导致重复写入。
        """

        response = await self._post(
            self._LIST_PATH,
            json={
                "pageNum": 1,
                "pageSize": 20,
                "datasetId": self.dataset_id,
                "searchText": document_id,
                "simple": True,
            },
        )
        payload = self._unwrap(response)
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise FastGPTResponseError(
                "FastGPT collection list response is missing data.data"
            )
        candidates = {document_id, f"{document_id}.txt"}
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("name") or "").strip() in candidates:
                collection_id = str(row.get("_id") or "").strip()
                if collection_id:
                    return collection_id
        return None

    async def _create_text_collection(self, document: KnowledgeDocument) -> str:
        response = await self._post(
            self._CREATE_PATH,
            json={
                "datasetId": self.dataset_id,
                "name": document.document_id,
                "text": document.text,
                "trainingType": self.training_type,
                # FastGPT 4.15+ 只接受 "auto" 或 "custom"。
                "chunkSettingMode": "auto",
                "dataEnhanceCollectionName": False,
                "metadata": dict(document.metadata),
            },
        )
        payload = self._unwrap(response)
        if not isinstance(payload, dict):
            raise FastGPTResponseError("FastGPT create returned unexpected payload")
        collection_id = str(payload.get("collectionId") or "").strip()
        if not collection_id:
            raise FastGPTResponseError("FastGPT create did not return collectionId")
        return collection_id

    async def _post(
        self,
        path: str,
        *,
        json: Mapping[str, Any],
    ) -> httpx.Response:
        try:
            response = await self.http_client.post(
                f"{self.base_url}{path}",
                headers=self._headers(),
                json=dict(json),
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise FastGPTTimeoutError(
                f"FastGPT request timed out: {path}"
            ) from exc
        except httpx.HTTPError as exc:
            raise FastGPTNetworkError(
                f"FastGPT transport error on {path}: {exc}"
            ) from exc

        self._raise_for_status(response, path=path)
        return response

    @staticmethod
    def _raise_for_status(response: httpx.Response, *, path: str) -> None:
        status = response.status_code
        if status < 400:
            return
        detail = response.text.strip()[:500]
        message = f"FastGPT HTTP {status} on {path}: {detail}"
        if status in (401, 403):
            raise FastGPTAuthenticationError(message)
        if status == 429:
            raise FastGPTRateLimitError(message)
        if status >= 500:
            raise FastGPTServerError(message)
        raise FastGPTRequestError(message)

    @staticmethod
    def _unwrap(response: httpx.Response) -> Any:
        try:
            body = response.json()
        except ValueError as exc:
            raise FastGPTResponseError("FastGPT returned a non-JSON body") from exc
        if not isinstance(body, dict):
            raise FastGPTResponseError("FastGPT returned an unexpected body")
        code = body.get("code")
        if code not in (None, 200):
            raise FastGPTResponseError(
                str(body.get("message") or "FastGPT request failed")
            )
        return body.get("data", body)
