"""Temporal Activity boundary for Data Loop application services."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Protocol

from temporalio import activity
from temporalio.exceptions import ApplicationError
from sqlalchemy.exc import SQLAlchemyError

from app.workflows.data_loop_contracts import (
    DataLoopStepCommand,
    DataLoopStepOutcome,
)


class DataLoopStepHandler(Protocol):
    async def execute(self, command: DataLoopStepCommand) -> DataLoopStepOutcome: ...


class DataLoopActivities:
    """Keep Temporal concerns outside the Data Loop domain implementation."""

    def __init__(self, handler: DataLoopStepHandler) -> None:
        self._handler = handler

    @activity.defn(name="run_data_loop_step")
    async def run_data_loop_step(
        self,
        command: DataLoopStepCommand,
    ) -> DataLoopStepOutcome:
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            if activity.in_activity():
                heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(command)
                )
            return await self._handler.execute(command)
        except ApplicationError:
            raise
        except Exception as exc:
            retryable = isinstance(exc, SQLAlchemyError) or bool(
                getattr(exc, "retryable", False)
            )
            message = str(exc).strip() or type(exc).__name__
            raise ApplicationError(
                message[:2000],
                type=type(exc).__name__,
                non_retryable=not retryable,
            ) from exc
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task

    @staticmethod
    async def _heartbeat_loop(command: DataLoopStepCommand) -> None:
        while True:
            activity.heartbeat(
                {
                    "step_type": command.step_type,
                    "step_key": command.step_key,
                    "run_idempotency_key": command.run_idempotency_key,
                }
            )
            await asyncio.sleep(10)
