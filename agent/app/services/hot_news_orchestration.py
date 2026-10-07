"""热点主流程：组织取数、基线、排行、证据准备和模型分析。"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import re
from typing import Protocol
from uuid import UUID

from app.analytics.baseline import NewsMetricBaseline
from app.analytics.data_source import BehaviorDataSource, BehaviorQuery
from app.analytics.entities import ContentType
from app.analytics.hot_news_enrichment import EnrichedHotNews, HotNewsEnrichmentService
from app.analytics.metric_source import HotNewsMetricQuery, NewsMetricSource
from app.analytics.metrics import NewsMetricCalculator, NewsMetricSnapshot
from app.analytics.ranking import HotNewsRanker, MetricKey, RankedHotNews
from app.model_runtime.result import AgentResult
from app.domain.errors import HotNewsDataQualityError
from app.schemas.hot_news import HotNewsAnalysisInput, HotNewsAnalysisReport
from app.services.hot_news_analysis import HotNewsAnalysisService
from app.services.hot_news_analysis_batch import HotNewsAnalysisBatch, HotNewsAnalysisBatchStats


@dataclass(frozen=True, slots=True)
class HotNewsRunRequest:
    """未来可直接作为单次 Temporal Workflow 输入的稳定运行参数。"""
    """通俗理解是告诉系统运行一次人物需要哪些基本信息：租户/时间窗口/版本/工作流版本"""

    tenant_id: str
    window_start: datetime
    window_end: datetime
    production_bundle_version: str
    workflow_version: str = "hot-news-workflow-v1"
    sql_query_id: str | None = None
    sql_query_user_id: str | None = None
    sql_query_scope_sha256: str | None = None

    def validate(self) -> None:
        if not self.tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        if not self.production_bundle_version.strip():
            raise ValueError("production_bundle_version cannot be empty")
        if not self.workflow_version.strip():
            raise ValueError("workflow_version cannot be empty")
        if self.window_start.tzinfo is None or self.window_start.utcoffset() is None:
            raise ValueError("window_start must be timezone-aware")
        if self.window_end.tzinfo is None or self.window_end.utcoffset() is None:
            raise ValueError("window_end must be timezone-aware")
        if self.window_start >= self.window_end:
            raise ValueError("window_start must be less than window_end")
        references = (self.sql_query_id, self.sql_query_user_id, self.sql_query_scope_sha256)
        if any(value is not None for value in references):
            if not all(value is not None for value in references):
                raise ValueError("SQL tool reference must include query, owner and scope")
            UUID(self.sql_query_id)
            UUID(self.sql_query_user_id)
            if not re.fullmatch(r"[0-9a-f]{64}", self.sql_query_scope_sha256):
                raise ValueError("SQL tool scope must be a SHA256 digest")

    @property
    def idempotency_key(self) -> str:
        """按租户、窗口和生产 Bundle 生成稳定且不暴露业务文本的键。"""

        identity = "\x1f".join(
            (
                self.tenant_id,
                self.window_start.astimezone(timezone.utc).isoformat(),
                self.window_end.astimezone(timezone.utc).isoformat(),
                self.production_bundle_version,
            )
        )
        # Preserve legacy keys, but never replay the old in-memory demo when
        # the same window now has a SQL-backed candidate query. Query UUID is
        # deliberately excluded: retries reuse the first atomically bound plan.
        if self.sql_query_scope_sha256 is not None:
            identity += "\x1fhot-news-sql-v1\x1f" + self.workflow_version + "\x1f" + self.sql_query_scope_sha256
        digest = sha256(identity.encode("utf-8")).hexdigest()
        return f"hot-news-{digest}"


@dataclass(frozen=True, slots=True)
class HotNewsOrchestrationPolicy:
    """随 Production Bundle 版本化的确定性运行参数。"""
    """通俗理解是：整个热点链路的业务规则：排行限制/相关性限制/候选集数量限制"""

    production_bundle_version: str
    content_types: frozenset[ContentType] = field(default_factory=frozenset)
    ranking_limit: int = 20
    related_limit: int = 3
    candidate_limit: int = 20
    source_guarantees_unique_event_id: bool = False

    def validate(self) -> None:
        if not self.production_bundle_version.strip():
            raise ValueError("production_bundle_version cannot be empty")
        if self.ranking_limit <= 0:
            raise ValueError("ranking_limit must be greater than 0")
        if self.related_limit <= 0:
            raise ValueError("related_limit must be greater than 0")
        if self.candidate_limit < self.related_limit:
            raise ValueError(
                "candidate_limit must be greater than or equal to related_limit"
            )


class HotNewsBaselineProvider(Protocol):
    """从已授权的聚合快照中读取当前窗口所需历史基线。"""
    """读取基线的接口"""

    async def get_baselines(
        self,
        *,
        tenant_id: str,
        window_start: datetime,
        window_end: datetime,
        production_bundle_version: str,
        metric_keys: frozenset[MetricKey],
    ) -> dict[MetricKey, NewsMetricBaseline]: ...


class EmptyHotNewsBaselineProvider:
    """显式的无基线实现；仅适合首次运行或本地联调。"""

    async def get_baselines(
        self,
        *,
        tenant_id: str,
        window_start: datetime,
        window_end: datetime,
        production_bundle_version: str,
        metric_keys: frozenset[MetricKey],
    ) -> dict[MetricKey, NewsMetricBaseline]:
        return {}


@dataclass(frozen=True, slots=True)
class AnalyzedHotNews:
    """供持久化的分析及输入快照；Workflow 只返回运行摘要。"""

    news_id: str
    rank: int
    analysis: AgentResult[HotNewsAnalysisReport]
    analysis_input: HotNewsAnalysisInput
    captured_at: datetime
    validated_at: datetime


@dataclass(frozen=True, slots=True)
class HotNewsRunResult:
    """单次热点运行结果；只保留聚合数据，不保留原始用户行为明细。"""

    request: HotNewsRunRequest
    idempotency_key: str
    fetched_record_count: int
    metric_snapshots: tuple[NewsMetricSnapshot, ...]
    baselines: tuple[NewsMetricBaseline, ...]
    ranked_news: tuple[RankedHotNews, ...]
    analyzed_news: tuple[AnalyzedHotNews, ...]
    sql_tool_trace: dict | None = None
    analysis_execution: HotNewsAnalysisBatchStats | None = None


class HotNewsRunService(Protocol):
    """Execution port used by the Temporal Activity.

    The concrete implementation may be a statically assembled service in
    tests or an active-Production-Bundle resolver in production.
    """

    async def run(self, request: HotNewsRunRequest) -> HotNewsRunResult: ...


class HotNewsOrchestrationService:
    """编排一次热点运行；重试、持久化和调度由外围基础设施负责。"""

    def __init__(
        self,
        *,
        baseline_provider: HotNewsBaselineProvider,
        enrichment_service: HotNewsEnrichmentService,
        analysis_service: HotNewsAnalysisService,
        policy: HotNewsOrchestrationPolicy,
        behavior_data_source: BehaviorDataSource | None = None,
        metric_source: NewsMetricSource | None = None,
        metric_calculator: NewsMetricCalculator | None = None,
        ranker: HotNewsRanker | None = None,
        analysis_batch: HotNewsAnalysisBatch | None = None,
    ) -> None:
        if (behavior_data_source is None) == (metric_source is None):
            raise ValueError(
                "exactly one of behavior_data_source or metric_source is required"
            )
        self.behavior_data_source = behavior_data_source
        self.metric_source = metric_source
        self.baseline_provider = baseline_provider
        self.enrichment_service = enrichment_service
        self.analysis_service = analysis_service
        self.policy = policy
        self.metric_calculator = metric_calculator or NewsMetricCalculator()
        self.ranker = ranker or HotNewsRanker()
        self.analysis_batch = analysis_batch or HotNewsAnalysisBatch()

    """核心运行位置"""
    async def run(self, request: HotNewsRunRequest) -> HotNewsRunResult:
        """执行查询、聚合、基线、排行、富化、模型分析和业务校验。"""

        request.validate()
        self.policy.validate()
        if request.production_bundle_version != self.policy.production_bundle_version:
            raise HotNewsDataQualityError(
                "run request production bundle does not match loaded policy: "
                f"request={request.production_bundle_version!r}, "
                f"policy={self.policy.production_bundle_version!r}"
            )
        if self.metric_source is not None:
            snapshots = await self.metric_source.fetch_snapshots(
                HotNewsMetricQuery(
                    tenant_id=request.tenant_id,
                    window_start=request.window_start,
                    window_end=request.window_end,
                    content_types=self.policy.content_types,
                    ranking_limit=self.policy.ranking_limit,
                )
            )
            fetched_record_count = len(snapshots)
        else:
            query = BehaviorQuery(
                start=request.window_start,
                end=request.window_end,
                tenant_id=request.tenant_id,
                content_types=self.policy.content_types,
            )
            records = await self.behavior_data_source.fetch(query)
            snapshots = self.metric_calculator.calculate(
                records,
                window_start=request.window_start,
                window_end=request.window_end,
                source_guarantees_unique_event_id=(
                    self.policy.source_guarantees_unique_event_id
                ),
            )
            fetched_record_count = len(records)
        metric_keys = frozenset(
            (snapshot.news_id, snapshot.content_type) for snapshot in snapshots
        )

        baselines = await self.baseline_provider.get_baselines(
            tenant_id=request.tenant_id,
            window_start=request.window_start,
            window_end=request.window_end,
            production_bundle_version=request.production_bundle_version,
            metric_keys=metric_keys,
        )
        self._validate_baselines(metric_keys=metric_keys, baselines=baselines)

        ranked = self.ranker.rank(
            snapshots,
            baselines,
            limit=self.policy.ranking_limit,
        )
        if not ranked:
            return self._empty_result(
                request=request,
                fetched_record_count=fetched_record_count,
                snapshots=snapshots,
                baselines=baselines,
            )

        enriched = await self.enrichment_service.enrich(
            ranked,
            tenant_id=request.tenant_id,
            related_limit=self.policy.related_limit,
            candidate_limit=self.policy.candidate_limit,
        )
        self._validate_enriched(ranked=ranked, enriched=enriched)

        executions, analysis_execution = await self.analysis_batch.analyze(
            enriched, service=self.analysis_service,
        )
        analyzed = [
            AnalyzedHotNews(
                news_id=item.ranking.current.news_id,
                rank=item.ranking.rank,
                analysis=execution.analysis,
                analysis_input=execution.analysis_input,
                captured_at=execution.captured_at,
                validated_at=execution.validated_at,
            )
            for item, execution in zip(enriched, executions, strict=True)
        ]

        """返回结果"""
        return HotNewsRunResult(
            request=request,
            idempotency_key=request.idempotency_key,
            fetched_record_count=fetched_record_count,
            metric_snapshots=tuple(snapshots),
            baselines=self._ordered_baselines(baselines),
            ranked_news=tuple(ranked),
            analyzed_news=tuple(analyzed),
            analysis_execution=analysis_execution,
        )

    @staticmethod
    def _validate_baselines(
        *,
        metric_keys: frozenset[MetricKey],
        baselines: dict[MetricKey, NewsMetricBaseline],
    ) -> None:
        for key, baseline in baselines.items():
            actual_key = (baseline.news_id, baseline.content_type)
            if key != actual_key:
                raise HotNewsDataQualityError(
                    "baseline key does not match baseline identity: "
                    f"key={key!r}, actual={actual_key!r}"
                )
            if key not in metric_keys:
                raise HotNewsDataQualityError(
                    f"baseline returned for news outside current window: {key!r}"
                )
            if baseline.sample_count <= 0:
                raise HotNewsDataQualityError(
                    f"baseline sample_count must be positive: {key!r}"
                )

    @staticmethod
    def _validate_enriched(
        *,
        ranked: list[RankedHotNews],
        enriched: list[EnrichedHotNews],
    ) -> None:
        expected = [
            (item.current.news_id, item.current.content_type) for item in ranked
        ]
        actual = [
            (item.ranking.current.news_id, item.ranking.current.content_type)
            for item in enriched
        ]
        if actual != expected:
            raise HotNewsDataQualityError(
                "enrichment output does not preserve ranking identity and order"
            )

        missing_content = [
            item.ranking.current.news_id
            for item in enriched
            if item.content is None
        ]
        if missing_content:
            raise HotNewsDataQualityError(
                "hot news content is missing for ranked news: "
                f"{missing_content}"
            )

        for item in enriched:
            content = item.content
            if content is None:
                continue
            try:
                content.validate()
            except ValueError as exc:
                raise HotNewsDataQualityError(
                    "hot news content failed validation: "
                    f"news_id={item.ranking.current.news_id!r}, error={exc}"
                ) from exc
            if content.news_id != item.ranking.current.news_id:
                raise HotNewsDataQualityError(
                    "hot news content identity does not match ranking: "
                    f"content={content.news_id!r}, "
                    f"ranking={item.ranking.current.news_id!r}"
                )

    @staticmethod
    def _ordered_baselines(
        baselines: dict[MetricKey, NewsMetricBaseline],
    ) -> tuple[NewsMetricBaseline, ...]:
        return tuple(
            baselines[key]
            for key in sorted(
                baselines,
                key=lambda item: (item[0], item[1].value),
            )
        )

    @classmethod
    def _empty_result(
        cls,
        *,
        request: HotNewsRunRequest,
        fetched_record_count: int,
        snapshots: list[NewsMetricSnapshot],
        baselines: dict[MetricKey, NewsMetricBaseline],
    ) -> HotNewsRunResult:
        return HotNewsRunResult(
            request=request,
            idempotency_key=request.idempotency_key,
            fetched_record_count=fetched_record_count,
            metric_snapshots=tuple(snapshots),
            baselines=cls._ordered_baselines(baselines),
            ranked_news=(),
            analyzed_news=(),
        )
