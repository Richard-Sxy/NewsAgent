"""行为数仓/数据查询 RPC 的强类型传输契约。"""

from typing import Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

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
from app.analytics.data_source import BehaviorDataSource, BehaviorQuery
from app.analytics.entities import BehaviorRecord, ContentType, EventType
from app.domain.errors import HotNewsDataQualityError


class BehaviorWatermarkRequest(BaseModel):
    """查询指定行为数据集当前可用的数据水位。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset: NonBlank128
    required_through: AwareDatetime


class BehaviorWatermarkResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset: NonBlank128
    available_through: AwareDatetime
    is_complete: bool
    meta: RpcResponseMeta


class BehaviorEventQueryRequest(BaseModel):
    """批量拉取一个左闭右开窗口内、分析所需的最小行为字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    window_start: AwareDatetime
    window_end: AwareDatetime
    news_ids: tuple[NonBlank128, ...] = ()
    content_types: tuple[Literal["article", "video"], ...] = ()
    page_size: int = Field(default=2_000, ge=1, le=10_000)
    page_token: NonBlank256 | None = None

    @model_validator(mode="after")
    def validate_window(self) -> "BehaviorEventQueryRequest":
        if self.window_start >= self.window_end:
            raise ValueError("window_start must be earlier than window_end")
        if len(self.news_ids) != len(set(self.news_ids)):
            raise ValueError("news_ids cannot contain duplicates")
        return self


class BehaviorEventRow(BaseModel):
    """企业字段的规范化传输对象；进入领域层前仍需显式枚举映射。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: NonBlank256
    anonymous_user_key: NonBlank256
    news_id: NonBlank128
    event_type: NonBlank128
    event_time: AwareDatetime
    content_type: NonBlank128
    duration_milliseconds: int = Field(default=0, ge=0)
    channel: NonBlank128 | None = None
    device_type: NonBlank128 | None = None


class BehaviorEventPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rows: tuple[BehaviorEventRow, ...]
    next_page_token: NonBlank256 | None = None
    watermark: AwareDatetime
    data_version: NonBlank128
    meta: RpcResponseMeta


class BehaviorWarehouseRpc(Protocol):
    """真实实现由企业 RPC SDK Adapter 提供。"""

    async def get_watermark(
        self,
        *,
        context: RpcCallContext,
        request: BehaviorWatermarkRequest,
    ) -> BehaviorWatermarkResponse: ...

    async def query_events(
        self,
        *,
        context: RpcCallContext,
        request: BehaviorEventQueryRequest,
    ) -> BehaviorEventPage: ...


class RpcBehaviorDataSource(BehaviorDataSource):
    """把企业行为 RPC 分页结果映射为热点计算领域对象。"""

    def __init__(
        self,
        client: BehaviorWarehouseRpc,
        *,
        dataset: str,
        context_provider: RpcCallContextProvider | None = None,
        event_type_mapping: dict[str, EventType] | None = None,
        content_type_mapping: dict[str, ContentType] | None = None,
        timeout_ms: int = 30_000,
        page_size: int = 2_000,
        max_pages: int = 100,
        max_records: int = 200_000,
    ) -> None:
        if not dataset.strip():
            raise ValueError("dataset cannot be empty")
        if not 1 <= page_size <= 10_000:
            raise ValueError("page_size must be between 1 and 10000")
        if max_pages <= 0 or max_records <= 0:
            raise ValueError("max_pages and max_records must be positive")
        self._client = client
        self._dataset = dataset.strip()
        self._context_provider = (
            context_provider or DefaultRpcCallContextProvider()
        )
        self._event_type_mapping = (
            {item.value: item for item in EventType}
            if event_type_mapping is None
            else dict(event_type_mapping)
        )
        self._content_type_mapping = (
            {item.value: item for item in ContentType}
            if content_type_mapping is None
            else dict(content_type_mapping)
        )
        self._timeout_ms = timeout_ms
        self._page_size = page_size
        self._max_pages = max_pages
        self._max_records = max_records

    async def fetch(self, query: BehaviorQuery) -> list[BehaviorRecord]:
        query.validate()
        if query.tenant_id is None:
            raise ValueError("tenant_id is required for enterprise behavior RPC")

        context = self._context_provider.create(
            tenant_id=query.tenant_id,
            operation="behavior-query",
            timeout_ms=self._timeout_ms,
        )
        budget = RpcDeadlineBudget(context)
        watermark = await self._client.get_watermark(
            context=budget.next_context(),
            request=BehaviorWatermarkRequest(
                dataset=self._dataset,
                required_through=query.end,
            ),
        )
        if watermark.dataset != self._dataset:
            raise EnterpriseRpcResponseError(
                "behavior watermark returned an unexpected dataset",
                request_id=watermark.meta.request_id,
            )
        if (
            not watermark.is_complete
            or watermark.available_through < query.end
        ):
            raise HotNewsDataQualityError(
                "behavior watermark does not cover requested window"
            )

        records: list[BehaviorRecord] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        data_version: str | None = None

        for _ in range(self._max_pages):
            page = await self._client.query_events(
                context=budget.next_context(),
                request=BehaviorEventQueryRequest(
                    window_start=query.start,
                    window_end=query.end,
                    news_ids=tuple(sorted(query.news_ids)),
                    content_types=tuple(
                        sorted(item.value for item in query.content_types)
                    ),
                    page_size=self._page_size,
                    page_token=page_token,
                ),
            )
            if page.watermark < query.end:
                raise HotNewsDataQualityError(
                    "behavior page watermark regressed below requested window"
                )
            if data_version is None:
                data_version = page.data_version
            elif page.data_version != data_version:
                raise HotNewsDataQualityError(
                    "behavior data version changed during pagination"
                )

            records.extend(self._to_domain(row, query) for row in page.rows)
            if len(records) > self._max_records:
                raise HotNewsDataQualityError(
                    "behavior query exceeded configured record limit"
                )

            next_token = page.next_page_token
            if next_token is None:
                return records
            if next_token in seen_tokens or next_token == page_token:
                raise EnterpriseRpcResponseError(
                    "behavior RPC returned a repeated page token",
                    request_id=page.meta.request_id,
                )
            seen_tokens.add(next_token)
            page_token = next_token

        raise HotNewsDataQualityError(
            "behavior query exceeded configured page limit"
        )

    def _to_domain(
        self,
        row: BehaviorEventRow,
        query: BehaviorQuery,
    ) -> BehaviorRecord:
        try:
            event_type = self._event_type_mapping[row.event_type]
            content_type = self._content_type_mapping[row.content_type]
        except KeyError as exc:
            raise EnterpriseRpcResponseError(
                f"unknown enterprise behavior enum: {exc.args[0]}"
            ) from exc

        if not (query.start <= row.event_time < query.end):
            raise EnterpriseRpcResponseError(
                "behavior RPC returned an event outside requested window"
            )
        if query.news_ids and row.news_id not in query.news_ids:
            raise EnterpriseRpcResponseError(
                "behavior RPC returned an unexpected news_id"
            )
        if query.content_types and content_type not in query.content_types:
            raise EnterpriseRpcResponseError(
                "behavior RPC returned an unexpected content_type"
            )

        record = BehaviorRecord(
            event_id=row.event_id,
            user_id=row.anonymous_user_key,
            news_id=row.news_id,
            event_type=event_type,
            event_time=row.event_time,
            content_type=content_type,
            duration_seconds=row.duration_milliseconds // 1000,
            channel=row.channel,
            device_type=row.device_type,
        )
        try:
            record.validate()
        except ValueError as exc:
            raise EnterpriseRpcResponseError(
                f"invalid enterprise behavior row: {exc}"
            ) from exc
        return record
