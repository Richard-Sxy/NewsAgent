"""通过真实 Activity 错误边界演练故障分类、重试和监控行为。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from temporalio.exceptions import ApplicationError

from app.activities.hot_news import HotNewsActivities
from app.clients.enterprise.common import (
    EnterpriseRpcAuthenticationError,
    EnterpriseRpcRateLimitError,
    EnterpriseRpcRequestError,
    EnterpriseRpcResponseError,
    EnterpriseRpcTimeoutError,
    EnterpriseRpcUnavailableError,
)
from app.domain.errors import (
    AgentOutputValidationError,
    HotNewsDataQualityError,
    HotNewsPersistenceError,
)
from app.observability.hot_news import InMemoryHotNewsRunMetrics
from app.services.hot_news_orchestration import HotNewsRunRequest


FaultType = Literal[
    "rpc_timeout",
    "rpc_unavailable",
    "rpc_rate_limit",
    "rpc_authentication",
    "rpc_request_invalid",
    "rpc_response_invalid",
    "watermark_incomplete",
    "data_version_drift",
    "model_output_invalid",
    "persistence_failure",
]
ExpectedAction = Literal["retry_once", "block_and_alert"]


class FaultScenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(min_length=1, max_length=128)
    operation: str = Field(min_length=1, max_length=128)
    fault_type: FaultType
    expected_retryable: bool
    expected_action: ExpectedAction
    alert_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_action(self) -> "FaultScenario":
        expected = "retry_once" if self.expected_retryable else "block_and_alert"
        if self.expected_action != expected:
            raise ValueError(
                "expected_action must match expected_retryable policy"
            )
        return self


class FaultDrillDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_name: str = Field(min_length=1, max_length=128)
    plan_version: str = Field(min_length=1, max_length=64)
    scenarios: tuple[FaultScenario, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scenario_ids(self) -> "FaultDrillDataset":
        values = [item.scenario_id for item in self.scenarios]
        if len(values) != len(set(values)):
            raise ValueError("fault scenario_id values must be unique")
        return self


class FaultDrillCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str
    passed: bool
    actual_error_type: str
    actual_retryable: bool
    actual_action: ExpectedAction
    metric_recorded: bool


class FaultDrillReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_name: str
    plan_version: str
    total_scenarios: int = Field(ge=1)
    passed_scenarios: int = Field(ge=0)
    pass_rate: float = Field(ge=0, le=1)
    cases: tuple[FaultDrillCaseResult, ...]


class _RaisingService:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def run(self, request: HotNewsRunRequest):
        raise self._error


class _EmptyRunStore:
    async def get_completed(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        window_start: datetime,
        window_end: datetime,
        production_bundle_version: str,
        workflow_version: str,
    ):
        return None

    async def save_completed(self, *, result):
        raise AssertionError("failed runs must not be persisted as completed")


class FaultDrillRunner:
    """执行本地故障演练；不会连接企业服务或写入数据库。"""

    async def run(self, dataset: FaultDrillDataset) -> FaultDrillReport:
        results = [
            await self._run_scenario(scenario)
            for scenario in dataset.scenarios
        ]
        passed = sum(item.passed for item in results)
        return FaultDrillReport(
            plan_name=dataset.plan_name,
            plan_version=dataset.plan_version,
            total_scenarios=len(results),
            passed_scenarios=passed,
            pass_rate=passed / len(results),
            cases=tuple(results),
        )

    async def _run_scenario(
        self,
        scenario: FaultScenario,
    ) -> FaultDrillCaseResult:
        metrics = InMemoryHotNewsRunMetrics()
        activities = HotNewsActivities(
            _RaisingService(self._build_error(scenario)),
            _EmptyRunStore(),
            run_metrics=metrics,
        )
        start = datetime(2026, 9, 9, 1, tzinfo=timezone.utc)
        request = HotNewsRunRequest(
            tenant_id="fault-drill-tenant",
            window_start=start,
            window_end=start + timedelta(hours=1),
            production_bundle_version="fault-drill-bundle-v1",
        )

        try:
            await activities.run_hot_news_window(request)
        except ApplicationError as exc:
            actual_retryable = not exc.non_retryable
            actual_action: ExpectedAction = (
                "retry_once" if actual_retryable else "block_and_alert"
            )
            error_type = exc.type or type(exc).__name__
        else:
            raise AssertionError("fault drill unexpectedly completed")

        metric_recorded = bool(
            metrics.events
            and metrics.events[-1].outcome == "failed"
            and metrics.events[-1].error_type == error_type
            and metrics.events[-1].retryable == actual_retryable
        )
        passed = all(
            (
                actual_retryable == scenario.expected_retryable,
                actual_action == scenario.expected_action,
                metric_recorded,
            )
        )
        return FaultDrillCaseResult(
            scenario_id=scenario.scenario_id,
            passed=passed,
            actual_error_type=error_type,
            actual_retryable=actual_retryable,
            actual_action=actual_action,
            metric_recorded=metric_recorded,
        )

    @staticmethod
    def _build_error(scenario: FaultScenario) -> Exception:
        message = f"fault drill: {scenario.scenario_id}"
        constructors = {
            "rpc_timeout": EnterpriseRpcTimeoutError,
            "rpc_unavailable": EnterpriseRpcUnavailableError,
            "rpc_rate_limit": EnterpriseRpcRateLimitError,
            "rpc_authentication": EnterpriseRpcAuthenticationError,
            "rpc_request_invalid": EnterpriseRpcRequestError,
            "rpc_response_invalid": EnterpriseRpcResponseError,
            "watermark_incomplete": HotNewsDataQualityError,
            "data_version_drift": HotNewsDataQualityError,
            "persistence_failure": HotNewsPersistenceError,
        }
        if scenario.fault_type == "model_output_invalid":
            return AgentOutputValidationError(
                message,
                raw_content="{invalid-model-output}",
                request_id="fault-drill-request",
            )
        return constructors[scenario.fault_type](message)


def load_fault_drill_dataset(path: str | Path) -> FaultDrillDataset:
    return FaultDrillDataset.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )
