"""热点分析 Temporal Activity 适配层。"""

import asyncio
from contextlib import suppress
from typing import Protocol

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.domain.errors import HotNewsDataQualityError
from app.services.hot_news_orchestration import (
    HotNewsOrchestrationService,
    HotNewsRunRequest,
    HotNewsRunResult,
)
from app.workflows.contracts import HotNewsActivityOutcome


class HotNewsRunStore(Protocol):
    """热点运行持久化接口；生产实现必须保证写入幂等。"""

    async def get_completed(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
    ) -> HotNewsActivityOutcome | None: ...

    async def save_completed(
        self,
        *,
        result: HotNewsRunResult,
    ) -> HotNewsActivityOutcome: ...


class HotNewsActivities:
    """在 Temporal 运行语义和热点业务编排之间做薄适配。"""

    def __init__(
        self,
        orchestration_service: HotNewsOrchestrationService,
        run_store: HotNewsRunStore,
    ) -> None:
        self._orchestration_service = orchestration_service
        self._run_store = run_store

    @activity.defn(name="run_hot_news_window")
    async def run_hot_news_window(
        self,
        request: HotNewsRunRequest,
    ) -> HotNewsActivityOutcome:
        """幂等执行并保存一个有边界的热点分析窗口。"""

        heartbeat_task: asyncio.Task[None] | None = None
        try:
            request.validate()

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
            )
            if completed is not None:
                self._validate_replayed_outcome(
                    request=request,
                    outcome=completed,
                )
                return completed

            result = await self._orchestration_service.run(request)
            return await self._run_store.save_completed(result=result)

        except ApplicationError:
            raise
        except Exception as exc:
            if activity.in_activity():
                activity.logger.exception(
                    "hot news window failed",
                    extra={
                        "tenant_id": request.tenant_id,
                        "idempotency_key": request.idempotency_key,
                        "error_type": type(exc).__name__,
                        "request_id": getattr(exc, "request_id", None),
                    },
                )
            raise self._to_application_error(exc) from exc
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task

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
