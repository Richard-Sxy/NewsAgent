"""热点分析 Temporal Activity 适配层。"""

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from typing import Protocol

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.domain.errors import (
    HotNewsAnalysisAttemptError,
    HotNewsDataQualityError,
)
from app.observability.hot_news import (
    HotNewsRunMetrics,
    NoopHotNewsRunMetrics,
)
from app.services.hot_news_orchestration import (
    HotNewsRunService,
    HotNewsRunRequest,
    HotNewsRunResult,
)
from app.services.hot_event_lifecycle import HotEventLifecycleService
from app.schemas.hot_news_events import (
    HotNewsProgressEvent,
    HotNewsProgressEventName,
    HotNewsProgressStatus,
)
from app.workflows.contracts import HotNewsActivityOutcome


class HotNewsRunStore(Protocol):
    """热点运行持久化接口；生产实现必须保证写入幂等。"""

    async def get_completed(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        window_start: datetime,
        window_end: datetime,
        production_bundle_version: str,
        workflow_version: str,
    ) -> HotNewsActivityOutcome | None: ...

    async def save_completed(
        self,
        *,
        result: HotNewsRunResult,
    ) -> HotNewsActivityOutcome: ...


class HotNewsFeedbackSink(Protocol):
    """Persist automatic feedback without leaking it into Temporal history."""

    async def collect_completed_run(
        self,
        *,
        result: HotNewsRunResult,
        run_id: str,
    ) -> None: ...

    async def collect_persisted_run(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        run_id: str,
    ) -> None: ...

    async def collect_analysis_failure(
        self,
        *,
        request: HotNewsRunRequest,
        error: HotNewsAnalysisAttemptError,
    ) -> None: ...


class HotNewsEventStream(Protocol):
    """热点事件推送端口；失败不得改变 PostgreSQL 业务结果。"""

    async def publish(
        self,
        event: HotNewsProgressEvent,
    ) -> HotNewsProgressEvent | None: ...


class HotNewsActivities:
    """在 Temporal 运行语义和热点业务编排之间做薄适配。"""

    def __init__(
        self,
        orchestration_service: HotNewsRunService,
        run_store: HotNewsRunStore,
        *,
        run_metrics: HotNewsRunMetrics | None = None,
        feedback_sink: HotNewsFeedbackSink | None = None,
        event_lifecycle: HotEventLifecycleService | None = None,
        event_stream: HotNewsEventStream | None = None,
    ) -> None:
        self._orchestration_service = orchestration_service
        self._run_store = run_store
        self._run_metrics = run_metrics or NoopHotNewsRunMetrics()
        self._feedback_sink = feedback_sink
        self._event_lifecycle = event_lifecycle
        self._event_stream = event_stream

    @activity.defn(name="run_hot_news_window")
    async def run_hot_news_window(
        self,
        request: HotNewsRunRequest,
    ) -> HotNewsActivityOutcome:
        """幂等执行并保存一个有边界的热点分析窗口。"""

        heartbeat_task: asyncio.Task[None] | None = None
        try:
            request.validate()
            await self._publish_progress(
                request=request,
                event="hot-news.run.started",
                status="running",
            )

            if activity.in_activity():
                activity.logger.info(
                    "starting hot news window",
                    extra={
                        "tenant_id": request.tenant_id,
                        "idempotency_key": request.idempotency_key,
                        "window_start": request.window_start.isoformat(),
                        "window_end": request.window_end.isoformat(),
                        "production_bundle_version": (
                            request.production_bundle_version
                        ),
                    },
                )
                heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(request.idempotency_key)
                )

            completed = await self._run_store.get_completed(
                tenant_id=request.tenant_id,
                idempotency_key=request.idempotency_key,
                window_start=request.window_start,
                window_end=request.window_end,
                production_bundle_version=(
                    request.production_bundle_version
                ),
                workflow_version=request.workflow_version,
            )
            if completed is not None:
                self._validate_replayed_outcome(
                    request=request,
                    outcome=completed,
                )
                if self._feedback_sink is not None:
                    await self._feedback_sink.collect_persisted_run(
                        tenant_id=request.tenant_id,
                        idempotency_key=request.idempotency_key,
                        run_id=completed.run_id,
                    )
                await self._publish_progress(
                    request=request,
                    event="hot-news.run.replayed",
                    status="replayed",
                    outcome=completed,
                )
                self._run_metrics.record("replayed")
                return completed

            result = await self._orchestration_service.run(request)
            outcome = await self._run_store.save_completed(result=result)
            if self._feedback_sink is not None:
                await self._feedback_sink.collect_completed_run(
                    result=result,
                    run_id=outcome.run_id,
                )
            await self._apply_event_lifecycle(request=request, result=result)
            await self._publish_progress(
                request=request,
                event="hot-news.run.completed",
                status="completed",
                outcome=outcome,
            )
            self._run_metrics.record("completed")
            return outcome

        except ApplicationError as exc:
            self._run_metrics.record(
                "failed",
                error_type=exc.type or "ApplicationError",
                retryable=not exc.non_retryable,
            )
            await self._publish_progress(
                request=request,
                event="hot-news.run.failed",
                status="failed",
                error_type=exc.type or "ApplicationError",
            )
            raise
        except Exception as exc:
            reported_error = exc
            if (
                isinstance(exc, HotNewsAnalysisAttemptError)
                and self._feedback_sink is not None
            ):
                try:
                    await self._feedback_sink.collect_analysis_failure(
                        request=request,
                        error=exc,
                    )
                except Exception as feedback_error:
                    # A transient feedback write may retry the bounded Activity
                    # once; the original validation error remains its cause.
                    reported_error = feedback_error
            retryable = bool(getattr(reported_error, "retryable", False))
            self._run_metrics.record(
                "failed",
                error_type=type(reported_error).__name__,
                retryable=retryable,
            )
            if activity.in_activity():
                activity.logger.exception(
                    "hot news window failed",
                    extra={
                        "tenant_id": request.tenant_id,
                        "idempotency_key": request.idempotency_key,
                        "error_type": type(reported_error).__name__,
                        "request_id": getattr(reported_error, "request_id", None),
                    },
                )
            await self._publish_progress(
                request=request,
                event="hot-news.run.failed",
                status="failed",
                error_type=type(reported_error).__name__,
            )
            raise self._to_application_error(reported_error) from reported_error
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task

    async def _publish_progress(
        self,
        *,
        request: HotNewsRunRequest,
        event: HotNewsProgressEventName,
        status: HotNewsProgressStatus,
        outcome: HotNewsActivityOutcome | None = None,
        error_type: str | None = None,
    ) -> None:
        """Best-effort publish; Redis is never the hot-news source of truth."""

        if self._event_stream is None:
            return
        try:
            await self._event_stream.publish(
                HotNewsProgressEvent(
                    event=event,
                    deduplication_key=(
                        f"{request.idempotency_key}:{status}"
                    ),
                    tenant_id=request.tenant_id,
                    run_key=request.idempotency_key,
                    run_id=None if outcome is None else outcome.run_id,
                    status=status,
                    production_bundle_version=(
                        request.production_bundle_version
                    ),
                    workflow_version=request.workflow_version,
                    window_start=request.window_start,
                    window_end=request.window_end,
                    counts=(
                        {}
                        if outcome is None
                        else {
                            "fetched_record_count": (
                                outcome.fetched_record_count
                            ),
                            "metric_snapshot_count": (
                                outcome.metric_snapshot_count
                            ),
                            "ranked_news_count": (
                                outcome.ranked_news_count
                            ),
                            "analyzed_news_count": (
                                outcome.analyzed_news_count
                            ),
                        }
                    ),
                    error_type=error_type,
                    occurred_at=datetime.now(timezone.utc),
                )
            )
        except Exception as exc:
            if activity.in_activity():
                activity.logger.warning(
                    "hot news Redis progress degraded",
                    extra={
                        "tenant_id": request.tenant_id,
                        "idempotency_key": request.idempotency_key,
                        "event": event,
                        "error_type": type(exc).__name__,
                    },
                )

    async def _apply_event_lifecycle(
        self,
        *,
        request: HotNewsRunRequest,
        result: HotNewsRunResult,
    ) -> None:
        """推进热点事件台账；失败只降级，不阻塞可信分析结果。"""

        if self._event_lifecycle is None:
            return
        try:
            await self._event_lifecycle.apply_completed_run(
                tenant_id=request.tenant_id,
                result=result,
            )
        except Exception as exc:
            if activity.in_activity():
                activity.logger.warning(
                    "hot event lifecycle degraded",
                    extra={
                        "tenant_id": request.tenant_id,
                        "idempotency_key": request.idempotency_key,
                        "error_type": type(exc).__name__,
                    },
                )

    @staticmethod
    def _validate_replayed_outcome(
        *,
        request: HotNewsRunRequest,
        outcome: HotNewsActivityOutcome,
    ) -> None:
        if outcome.idempotency_key != request.idempotency_key:
            raise HotNewsDataQualityError(
                "persisted hot news outcome has mismatched idempotency key"
            )
        if outcome.status != "completed":
            raise HotNewsDataQualityError(
                "only completed hot news outcome can be replayed"
            )

    @staticmethod
    def _to_application_error(exc: Exception) -> ApplicationError:
        message = str(exc).strip() or type(exc).__name__
        retryable = bool(getattr(exc, "retryable", False))
        return ApplicationError(
            message[:2000],
            type=type(exc).__name__,
            non_retryable=not retryable,
        )

    @staticmethod
    async def _heartbeat_loop(idempotency_key: str) -> None:
        while True:
            activity.heartbeat(
                {
                    "phase": "running",
                    "idempotency_key": idempotency_key,
                }
            )
            await asyncio.sleep(10)
