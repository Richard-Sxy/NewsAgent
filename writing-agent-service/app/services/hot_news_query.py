"""热点运营控制台的只读查询服务。

只读取 ``analysis_runs`` 的不可变结果快照与 ``hot_news_decisions`` 决策
记录，不做任何重新计算；榜单、热度分量与分析摘要的权威来源是持久化的
``result_payload``（schema 版本 2.0）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import Database
from app.domain.errors import HotNewsPersistenceError
from app.models.hot_news import HotNewsAnalysisRun
from app.models.hot_news_decision import HotNewsDecision
from app.schemas.hot_news_api import (
    HotNewsAnalysisSummaryView,
    HotNewsDecisionView,
    HotNewsMetricSnapshotView,
    HotNewsRankedItemView,
    HotNewsRunDetailResponse,
    HotNewsRunSummary,
    HotScoreView,
)


class HotNewsQueryService:
    """为热点只读 API 提供运行列表与单运行详情。"""

    def __init__(self, *, database: Database) -> None:
        self._database = database

    async def list_runs(
        self,
        *,
        tenant_id: str,
        offset: int,
        limit: int,
    ) -> tuple[HotNewsRunSummary, ...]:
        """按窗口倒序列出租户的热点运行；不包含结果载荷。"""

        try:
            async with self._database.session() as session:
                statement = (
                    select(HotNewsAnalysisRun)
                    .where(HotNewsAnalysisRun.tenant_id == tenant_id)
                    .order_by(
                        HotNewsAnalysisRun.window_start.desc(),
                        HotNewsAnalysisRun.completed_at.desc(),
                    )
                    .offset(offset)
                    .limit(limit)
                )
                db_result = await session.execute(statement)
                runs = db_result.scalars().all()
                return tuple(self._to_summary(run) for run in runs)
        except SQLAlchemyError as exc:
            raise HotNewsPersistenceError("查询热点运行列表失败") from exc

    async def get_run_detail(
        self,
        *,
        tenant_id: str,
        run_id: UUID,
    ) -> HotNewsRunDetailResponse | None:
        """读取一次运行的榜单、热度分量、分析摘要与决策记录。"""

        try:
            async with self._database.session() as session:
                statement = select(HotNewsAnalysisRun).where(
                    HotNewsAnalysisRun.id == run_id,
                    HotNewsAnalysisRun.tenant_id == tenant_id,
                    HotNewsAnalysisRun.status == "completed",
                )
                db_result = await session.execute(statement)
                run = db_result.scalar_one_or_none()
                if run is None:
                    return None

                decisions_result = await session.execute(
                    select(HotNewsDecision)
                    .where(
                        HotNewsDecision.tenant_id == tenant_id,
                        HotNewsDecision.run_id == run_id,
                    )
                    .order_by(HotNewsDecision.created_at.asc())
                )
                decisions = decisions_result.scalars().all()

                return HotNewsRunDetailResponse(
                    run=self._to_summary(run),
                    ranked_news=self._parse_ranked_news(run),
                    decisions=tuple(
                        self._to_decision_view(item) for item in decisions
                    ),
                )
        except SQLAlchemyError as exc:
            raise HotNewsPersistenceError("查询热点运行详情失败") from exc

    @staticmethod
    def _to_summary(run: HotNewsAnalysisRun) -> HotNewsRunSummary:
        return HotNewsRunSummary(
            run_id=run.id,
            idempotency_key=run.idempotency_key,
            window_start=run.window_start,
            window_end=run.window_end,
            production_bundle_version=run.production_bundle_version,
            workflow_version=run.workflow_version,
            status=run.status,
            fetched_record_count=run.fetched_record_count,
            metric_snapshot_count=run.metric_snapshot_count,
            ranked_news_count=run.ranked_news_count,
            analyzed_news_count=run.analyzed_news_count,
            completed_at=run.completed_at,
        )

    @classmethod
    def _parse_ranked_news(
        cls,
        run: HotNewsAnalysisRun,
    ) -> tuple[HotNewsRankedItemView, ...]:
        if run.payload_schema_version != "2.0":
            raise HotNewsPersistenceError(
                f"热点运行结果快照版本不受支持：{run.payload_schema_version}"
            )
        payload = run.result_payload
        if not isinstance(payload, dict):
            raise HotNewsPersistenceError("热点运行的 result_payload 格式错误")

        ranked_news = payload.get("ranked_news")
        if not isinstance(ranked_news, list):
            raise HotNewsPersistenceError("热点运行的 ranked_news 格式错误")

        analyzed_by_news_id = cls._index_analyzed_news(payload)
        titles_by_news_id = cls._index_news_titles(payload)

        items: list[HotNewsRankedItemView] = []
        for entry in ranked_news:
            if not isinstance(entry, dict):
                raise HotNewsPersistenceError("热点榜单记录格式错误")
            try:
                metrics = cls._parse_metrics(entry["current"])
                hot_score = cls._parse_hot_score(entry["hot_score"])
                baseline = entry.get("baseline")
                if baseline is not None and not isinstance(baseline, dict):
                    raise HotNewsPersistenceError("热点基线引用格式错误")
                items.append(
                    HotNewsRankedItemView(
                        rank=int(entry["rank"]),
                        news_id=metrics.news_id,
                        title=titles_by_news_id.get(metrics.news_id),
                        metrics=metrics,
                        baseline=baseline,
                        hot_score=hot_score,
                        analysis=analyzed_by_news_id.get(metrics.news_id),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise HotNewsPersistenceError(
                    "热点榜单记录不能通过 Schema 校验"
                ) from exc
        return tuple(items)

    @staticmethod
    def _parse_metrics(raw: Any) -> HotNewsMetricSnapshotView:
        if not isinstance(raw, dict):
            raise HotNewsPersistenceError("热点指标快照格式错误")
        return HotNewsMetricSnapshotView(
            news_id=raw["news_id"],
            content_type=raw["content_type"],
            window_start=raw["window_start"],
            window_end=raw["window_end"],
            impressions=int(raw["impressions"]),
            clicks=int(raw["clicks"]),
            unique_users=int(raw["unique_users"]),
            total_duration_seconds=int(raw["total_duration_seconds"]),
            effective_consumptions=int(raw["effective_consumptions"]),
            interactions=int(raw["interactions"]),
            ctr=str(raw["ctr"]),
        )

    @staticmethod
    def _parse_hot_score(raw: Any) -> HotScoreView:
        if not isinstance(raw, dict):
            raise HotNewsPersistenceError("热度评分记录格式错误")
        return HotScoreView(
            score=str(raw["score"]),
            click_component=str(raw["click_component"]),
            consumption_component=str(raw["consumption_component"]),
            interaction_component=str(raw["interaction_component"]),
            growth_component=str(raw["growth_component"]),
        )

    @classmethod
    def _index_analyzed_news(
        cls,
        payload: dict[str, Any],
    ) -> dict[str, HotNewsAnalysisSummaryView]:
        analyzed_news = payload.get("analyzed_news")
        if analyzed_news is None:
            return {}
        if not isinstance(analyzed_news, list):
            raise HotNewsPersistenceError("热点运行的 analyzed_news 格式错误")

        indexed: dict[str, HotNewsAnalysisSummaryView] = {}
        for item in analyzed_news:
            if not isinstance(item, dict):
                raise HotNewsPersistenceError("热点分析记录格式错误")
            analysis = item.get("analysis")
            if not isinstance(analysis, dict):
                raise HotNewsPersistenceError("热点分析结果格式错误")
            report = analysis.get("value")
            if not isinstance(report, dict):
                raise HotNewsPersistenceError("热点分析报告格式错误")
            try:
                indexed[item["news_id"]] = HotNewsAnalysisSummaryView(
                    trend_assessment=report["trend_assessment"],
                    dominant_driver=report["dominant_driver"],
                    attention_reasons=tuple(
                        report.get("attention_reasons") or ()
                    ),
                    operation_suggestions=tuple(
                        report.get("operation_suggestions") or ()
                    ),
                    limitations=tuple(report.get("limitations") or ()),
                    evidence_news_ids=tuple(
                        report.get("evidence_news_ids") or ()
                    ),
                    overall_confidence=float(report["overall_confidence"]),
                    fastgpt_request_id=analysis.get("request_id"),
                    validated_at=item["validated_at"],
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise HotNewsPersistenceError(
                    "热点分析记录不能通过 Schema 校验"
                ) from exc
        return indexed

    @staticmethod
    def _index_news_titles(payload: dict[str, Any]) -> dict[str, str]:
        """从已持久化的可信分析输入快照中投影新闻标题。"""

        analyzed_news = payload.get("analyzed_news")
        if not isinstance(analyzed_news, list):
            return {}

        indexed: dict[str, str] = {}
        for item in analyzed_news:
            if not isinstance(item, dict):
                continue
            news_id = item.get("news_id")
            analysis_input = item.get("analysis_input")
            if not isinstance(news_id, str) or not isinstance(
                analysis_input,
                dict,
            ):
                continue
            title = analysis_input.get("title")
            if isinstance(title, str) and title.strip():
                indexed[news_id] = title.strip()
        return indexed

    @staticmethod
    def _to_decision_view(item: HotNewsDecision) -> HotNewsDecisionView:
        return HotNewsDecisionView(
            decision_id=item.id,
            news_id=item.news_id,
            decision_type=item.decision_type,
            reason=item.reason,
            correction_payload=item.correction_payload,
            operator_id=item.operator_id,
            idempotency_key=item.idempotency_key,
            supersedes_decision_id=item.supersedes_decision_id,
            created_at=item.created_at,
        )
