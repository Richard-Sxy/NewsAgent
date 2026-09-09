"""历史指标基线 RPC 的批量查询契约。"""

from decimal import Decimal
from datetime import datetime
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
from app.analytics.baseline import NewsMetricBaseline
from app.analytics.entities import ContentType
from app.domain.errors import HotNewsDataQualityError
from app.services.hot_news_orchestration import (
    HotNewsBaselineProvider,
    MetricKey,
)


class BaselineMetricKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    news_id: NonBlank128
    content_type: Literal["article", "video"]


class MetricBaselineQueryRequest(BaseModel):
    """查询当前窗口对应的可比历史基线，不传输用户行为明细。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    current_window_start: AwareDatetime
    current_window_end: AwareDatetime
    metric_keys: tuple[BaselineMetricKey, ...] = Field(min_length=1)
    baseline_policy_version: NonBlank128
    page_size: int = Field(default=1_000, ge=1, le=5_000)
    page_token: NonBlank256 | None = None

    @model_validator(mode="after")
    def validate_request(self) -> "MetricBaselineQueryRequest":
        if self.current_window_start >= self.current_window_end:
            raise ValueError(
                "current_window_start must be earlier than current_window_end"
            )
        identities = {
            (item.news_id, item.content_type) for item in self.metric_keys
        }
        if len(identities) != len(self.metric_keys):
            raise ValueError("metric_keys cannot contain duplicates")
        return self


class MetricBaselineRow(BaseModel):
    """数仓已经聚合的历史窗口均值。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    news_id: NonBlank128
    content_type: Literal["article", "video"]
    sample_count: int = Field(ge=1)
    impressions: Decimal = Field(ge=0)
    clicks: Decimal = Field(ge=0)
    unique_users: Decimal = Field(ge=0)
    total_duration_seconds: Decimal = Field(ge=0)
    effective_consumptions: Decimal = Field(ge=0)
    interactions: Decimal = Field(ge=0)
    ctr: Decimal = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_counts(self) -> "MetricBaselineRow":
        if self.clicks > self.impressions:
            raise ValueError("clicks cannot exceed impressions")
        if self.unique_users > self.impressions:
            raise ValueError("unique_users cannot exceed impressions")
        return self


class MetricBaselinePage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rows: tuple[MetricBaselineRow, ...]
    next_page_token: NonBlank256 | None = None
    baseline_policy_version: NonBlank128
    data_version: NonBlank128
    meta: RpcResponseMeta


class MetricBaselineWarehouseRpc(Protocol):
    """企业历史聚合表或指标服务的 RPC Port。"""

    async def query_baselines(
        self,
        *,
        context: RpcCallContext,
        request: MetricBaselineQueryRequest,
    ) -> MetricBaselinePage: ...


class RpcHotNewsBaselineProvider(HotNewsBaselineProvider):
    """将企业聚合基线分页响应转换为热点计算领域基线。"""

    def __init__(
        self,
        client: MetricBaselineWarehouseRpc,
        *,
        production_bundle_version: str,
        baseline_policy_version: str,
        context_provider: RpcCallContextProvider | None = None,
        timeout_ms: int = 15_000,
        page_size: int = 1_000,
        max_pages: int = 20,
    ) -> None:
        if not production_bundle_version.strip():
            raise ValueError("production_bundle_version cannot be empty")
        if not baseline_policy_version.strip():
            raise ValueError("baseline_policy_version cannot be empty")
        if not 1 <= page_size <= 5_000:
            raise ValueError("page_size must be between 1 and 5000")
        if max_pages <= 0:
            raise ValueError("max_pages must be positive")
        self._client = client
        self._production_bundle_version = production_bundle_version.strip()
        self._baseline_policy_version = baseline_policy_version.strip()
        self._context_provider = (
            context_provider or DefaultRpcCallContextProvider()
        )
        self._timeout_ms = timeout_ms
        self._page_size = page_size
        self._max_pages = max_pages

    async def get_baselines(
        self,
        *,
        tenant_id: str,
        window_start: datetime,
        window_end: datetime,
        production_bundle_version: str,
        metric_keys: frozenset[MetricKey],
    ) -> dict[MetricKey, NewsMetricBaseline]:
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        if production_bundle_version != self._production_bundle_version:
            raise HotNewsDataQualityError(
                "baseline provider production bundle version mismatch"
            )
        if not metric_keys:
            return {}

        context = self._context_provider.create(
            tenant_id=tenant_id,
            operation="metric-baseline-query",
            timeout_ms=self._timeout_ms,
        )
        budget = RpcDeadlineBudget(context)
        page_token: str | None = None
        seen_tokens: set[str] = set()
        data_version: str | None = None
        baselines: dict[MetricKey, NewsMetricBaseline] = {}
        requested_keys = set(metric_keys)

        transport_keys = tuple(
            BaselineMetricKey(
                news_id=news_id,
                content_type=content_type.value,
            )
            for news_id, content_type in sorted(
                metric_keys,
                key=lambda item: (item[0], item[1].value),
            )
        )

        for _ in range(self._max_pages):
            page = await self._client.query_baselines(
                context=budget.next_context(),
                request=MetricBaselineQueryRequest(
                    current_window_start=window_start,
                    current_window_end=window_end,
                    metric_keys=transport_keys,
                    baseline_policy_version=self._baseline_policy_version,
                    page_size=self._page_size,
                    page_token=page_token,
                ),
            )
            if page.baseline_policy_version != self._baseline_policy_version:
                raise HotNewsDataQualityError(
                    "baseline RPC returned an unexpected policy version"
                )
            if data_version is None:
                data_version = page.data_version
            elif page.data_version != data_version:
                raise HotNewsDataQualityError(
                    "baseline data version changed during pagination"
                )

            for row in page.rows:
                key = (row.news_id, ContentType(row.content_type))
                if key not in requested_keys:
                    raise EnterpriseRpcResponseError(
                        "baseline RPC returned an unrequested metric key",
                        request_id=page.meta.request_id,
                    )
                if key in baselines:
                    raise EnterpriseRpcResponseError(
                        "baseline RPC returned a duplicate metric key",
                        request_id=page.meta.request_id,
                    )
                baselines[key] = NewsMetricBaseline(
                    news_id=row.news_id,
                    content_type=ContentType(row.content_type),
                    sample_count=row.sample_count,
                    impressions=row.impressions,
                    clicks=row.clicks,
                    unique_users=row.unique_users,
                    total_duration_seconds=row.total_duration_seconds,
                    effective_consumptions=row.effective_consumptions,
                    interactions=row.interactions,
                    ctr=row.ctr,
                )

            next_token = page.next_page_token
            if next_token is None:
                return baselines
            if next_token in seen_tokens or next_token == page_token:
                raise EnterpriseRpcResponseError(
                    "baseline RPC returned a repeated page token",
                    request_id=page.meta.request_id,
                )
            seen_tokens.add(next_token)
            page_token = next_token

        raise HotNewsDataQualityError(
            "baseline query exceeded configured page limit"
        )
