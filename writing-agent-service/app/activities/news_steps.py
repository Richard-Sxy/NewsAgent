import asyncio
from contextlib import suppress
from typing import Protocol

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.workflows.contracts import JobStateCommand, StepCommand, StepOutcome


class StepHandler(Protocol):
    async def __call__(self, command: StepCommand) -> StepOutcome: ...


class StateHandler(Protocol):
    async def __call__(self, command: JobStateCommand) -> None: ...


class NewsStepActivities:
    """Temporal 与业务执行层的适配器。

    handler 负责：建立/恢复 step 和 agent_run、调用 FastGPT、写入 S3，
    最后在 PostgreSQL 中原子提交 checkpoint。
    """

    def __init__(
        self,
        handler: StepHandler,
        state_handler: StateHandler | None = None,
    ) -> None:
        self._handler = handler
        self._state_handler = state_handler

    @activity.defn(name="run_news_step")
    async def run_news_step(self, command: StepCommand) -> StepOutcome:
        activity.logger.info(
            "executing news step",
            extra={
                "job_id": command.job_id,
                "step_key": command.step_key,
                "attempt": command.attempt,
            },
        )
        # 心跳机制，等待 10s 以后创建
        heartbeat_task = (
            asyncio.create_task(self._heartbeat_loop())
            if activity.in_activity()
            else None
        )
        try:
            return await self._handler(command)
        except ApplicationError:
            raise
        except Exception as exc:
            raise ApplicationError(
                str(exc) or type(exc).__name__,
                type=type(exc).__name__,
                non_retryable=not bool(getattr(exc, "retryable", False)),
            ) from exc
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task

    @activity.defn(name="transition_job_state")
    async def transition_job_state(self, command: JobStateCommand) -> None:
        if self._state_handler is None:
            raise ApplicationError("state handler is not configured", non_retryable=True)
        await self._state_handler(command)

    @staticmethod
    async def _heartbeat_loop() -> None:
        while True:
            activity.heartbeat("agent step is running")
            await asyncio.sleep(10)
