"""Temporal Activity boundary for Data Loop application services."""

from __future__ import annotations

from typing import Protocol

from temporalio import activity
from temporalio.exceptions import ApplicationError

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
        try:
            return await self._handler.execute(command)
        except ApplicationError:
            raise
        except Exception as exc:
            retryable = bool(getattr(exc, "retryable", False))
            message = str(exc).strip() or type(exc).__name__
            raise ApplicationError(
                message[:2000],
                type=type(exc).__name__,
                non_retryable=not retryable,
            ) from exc
