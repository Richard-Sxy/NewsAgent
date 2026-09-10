"""热点分析 Agent 的版本化离线评测契约与执行器。"""

from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.clients.fastgpt import AgentResult
from app.domain.errors import AgentOutputValidationError
from app.schemas.hot_news import (
    DriverType,
    HotNewsAnalysisInput,
    HotNewsAnalysisReport,
    MetricKey,
)
from app.services.hot_news_analysis import HotNewsAnalysisValidator


class ExpectedHotNewsAnalysis(BaseModel):
    """人工给出的最小验收标签，不要求编写一份完整标准答案。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_dominant_drivers: tuple[DriverType, ...] = Field(min_length=1)
    required_evidence_news_ids: tuple[str, ...] = ()
    forbidden_evidence_news_ids: tuple[str, ...] = ()
    required_metric_keys: tuple[MetricKey, ...] = ()
    must_state_limitation: bool = False

    @model_validator(mode="after")
    def validate_labels(self) -> "ExpectedHotNewsAnalysis":
        if len(self.allowed_dominant_drivers) != len(
            set(self.allowed_dominant_drivers)
        ):
            raise ValueError("allowed_dominant_drivers cannot contain duplicates")
        required = set(self.required_evidence_news_ids)
        forbidden = set(self.forbidden_evidence_news_ids)
        if required & forbidden:
            raise ValueError("required and forbidden evidence cannot overlap")
        if any(not item.strip() for item in required | forbidden):
            raise ValueError("evidence news_ids cannot be empty")
        return self


class HotNewsEvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=500)
    tags: tuple[str, ...] = Field(default=(), max_length=20)
    analysis_input: HotNewsAnalysisInput
    expected: ExpectedHotNewsAnalysis

    @model_validator(mode="after")
    def validate_expected_evidence(self) -> "HotNewsEvaluationCase":
        available = {
            item.news_id for item in self.analysis_input.related_news
        }
        missing = set(self.expected.required_evidence_news_ids) - available
        if missing:
            raise ValueError(
                "required evidence is absent from analysis_input: "
                f"{sorted(missing)}"
            )
        return self


class HotNewsEvaluationDataset(BaseModel):
    """可以按日期冻结、计算哈希并重复运行的评测数据集。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_name: str = Field(min_length=1, max_length=128)
    dataset_version: str = Field(min_length=1, max_length=64)
    schema_version: str = Field(default="1.0", pattern=r"^1\.0$")
    description: str = Field(min_length=1, max_length=1000)
    cases: tuple[HotNewsEvaluationCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_ids(self) -> "HotNewsEvaluationDataset":
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("dataset case_id values must be unique")
        return self

    @property
    def content_sha256(self) -> str:
        payload = self.model_dump_json(
            exclude_none=False,
            by_alias=True,
        ).encode("utf-8")
        return sha256(payload).hexdigest()


class HotNewsEvaluationRunner(Protocol):
    async def run(
        self,
        analysis_input: HotNewsAnalysisInput,
    ) -> AgentResult[HotNewsAnalysisReport]: ...


class HotNewsCaseEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    passed: bool
    contract_valid: bool
    dominant_driver_match: bool
    required_evidence_recall: float = Field(ge=0, le=1)
    forbidden_evidence_count: int = Field(ge=0)
    required_metric_coverage: float = Field(ge=0, le=1)
    limitation_match: bool
    error_type: str | None = None
    error_message: str | None = None


class HotNewsEvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_name: str
    dataset_version: str
    dataset_sha256: str
    total_cases: int = Field(ge=1)
    passed_cases: int = Field(ge=0)
    pass_rate: float = Field(ge=0, le=1)
    contract_pass_rate: float = Field(ge=0, le=1)
    dominant_driver_accuracy: float = Field(ge=0, le=1)
    mean_required_evidence_recall: float = Field(ge=0, le=1)
    mean_required_metric_coverage: float = Field(ge=0, le=1)
    cases: tuple[HotNewsCaseEvaluation, ...]


class HotNewsEvaluationService:
    """运行模型并用确定性规则计算评测结果；模型不参与打分。"""

    def __init__(
        self,
        runner: HotNewsEvaluationRunner,
        validator: HotNewsAnalysisValidator | None = None,
        *,
        concurrency_limiter: asyncio.Semaphore | None = None,
    ) -> None:
        self._runner = runner
        self._validator = validator or HotNewsAnalysisValidator()
        self._concurrency_limiter = concurrency_limiter

    async def evaluate(
        self,
        dataset: HotNewsEvaluationDataset,
    ) -> HotNewsEvaluationReport:
        if self._concurrency_limiter is None:
            results = [
                await self._evaluate_case(case) for case in dataset.cases
            ]
        else:
            # asyncio.gather preserves input order while the shared semaphore
            # bounds aggregate FastGPT pressure across every cohort/baseline.
            results = list(
                await asyncio.gather(
                    *(
                        self._evaluate_case_bounded(case)
                        for case in dataset.cases
                    )
                )
            )

        total = len(results)
        return HotNewsEvaluationReport(
            dataset_name=dataset.dataset_name,
            dataset_version=dataset.dataset_version,
            dataset_sha256=dataset.content_sha256,
            total_cases=total,
            passed_cases=sum(item.passed for item in results),
            pass_rate=sum(item.passed for item in results) / total,
            contract_pass_rate=(
                sum(item.contract_valid for item in results) / total
            ),
            dominant_driver_accuracy=(
                sum(item.dominant_driver_match for item in results) / total
            ),
            mean_required_evidence_recall=(
                sum(item.required_evidence_recall for item in results) / total
            ),
            mean_required_metric_coverage=(
                sum(item.required_metric_coverage for item in results) / total
            ),
            cases=tuple(results),
        )

    async def _evaluate_case_bounded(
        self,
        case: HotNewsEvaluationCase,
    ) -> HotNewsCaseEvaluation:
        assert self._concurrency_limiter is not None
        async with self._concurrency_limiter:
            return await self._evaluate_case(case)

    async def _evaluate_case(
        self,
        case: HotNewsEvaluationCase,
    ) -> HotNewsCaseEvaluation:
        try:
            result = await self._runner.run(case.analysis_input)
            self._validator.validate(
                analysis_input=case.analysis_input,
                result=result,
            )
        except AgentOutputValidationError as exc:
            return HotNewsCaseEvaluation(
                case_id=case.case_id,
                passed=False,
                contract_valid=False,
                dominant_driver_match=False,
                required_evidence_recall=0,
                forbidden_evidence_count=0,
                required_metric_coverage=0,
                limitation_match=False,
                error_type=type(exc).__name__,
                error_message=(str(exc).strip() or type(exc).__name__)[:500],
            )

        report = result.value
        expected = case.expected
        referenced_evidence = self._referenced_evidence(report)
        referenced_metrics = self._referenced_metrics(report)
        required_evidence = set(expected.required_evidence_news_ids)
        required_metrics = set(expected.required_metric_keys)
        evidence_recall = self._coverage(
            required_evidence,
            referenced_evidence,
        )
        metric_coverage = self._coverage(
            required_metrics,
            referenced_metrics,
        )
        forbidden_count = len(
            referenced_evidence & set(expected.forbidden_evidence_news_ids)
        )
        driver_match = (
            report.dominant_driver in expected.allowed_dominant_drivers
        )
        limitation_match = (
            bool(report.limitations)
            if expected.must_state_limitation
            else True
        )
        passed = all(
            (
                driver_match,
                evidence_recall == 1,
                forbidden_count == 0,
                metric_coverage == 1,
                limitation_match,
            )
        )
        return HotNewsCaseEvaluation(
            case_id=case.case_id,
            passed=passed,
            contract_valid=True,
            dominant_driver_match=driver_match,
            required_evidence_recall=evidence_recall,
            forbidden_evidence_count=forbidden_count,
            required_metric_coverage=metric_coverage,
            limitation_match=limitation_match,
        )

    @staticmethod
    def _referenced_evidence(report: HotNewsAnalysisReport) -> set[str]:
        values = set(report.evidence_news_ids)
        for reason in report.attention_reasons:
            values.update(reason.evidence_news_ids)
        for context in report.related_contexts:
            values.update(context.evidence_news_ids)
        for suggestion in report.operation_suggestions:
            values.update(suggestion.evidence_news_ids)
        return values

    @staticmethod
    def _referenced_metrics(report: HotNewsAnalysisReport) -> set[str]:
        values: set[str] = set()
        for reason in report.attention_reasons:
            values.update(reason.metric_keys)
        for suggestion in report.operation_suggestions:
            values.update(suggestion.metric_keys)
        return values

    @staticmethod
    def _coverage(required: set[str], actual: set[str]) -> float:
        return 1 if not required else len(required & actual) / len(required)


def load_evaluation_dataset(path: str | Path) -> HotNewsEvaluationDataset:
    return HotNewsEvaluationDataset.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )
