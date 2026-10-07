"""Analyze complete ranked runs referenced by a frozen evaluation dataset.

Feedback cases select evidence; they are neither a full ranked snapshot nor an
unbiased quality sample. This service keeps their coverage separate from fixed
overview/quality calculations over each immutable source run.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Protocol, get_args
from uuid import UUID

from app.data_analysis.engine import (
    ALGORITHM_VERSION,
    AnalysisInputError,
    validate_request,
)
from app.data_analysis.projection import AnalysisProjectionError, project_dataset
from app.data_analysis.runner import AnalysisExecutionError
from app.schemas.evaluation_dataset import (
    EvaluationDatasetManifest,
    FrozenEvaluationCase,
    canonical_json_bytes,
)
from app.schemas.hot_news_api import HotNewsRunDetailResponse
from app.schemas.analysis_feedback import FeedbackProblemType, FeedbackSeverity, FeedbackSource


DATASET_ANALYSIS_REPORT_VERSION = "data-loop-dataset-analysis-v1"
_COUNTS = (
    "impressions", "clicks", "unique_users", "effective_consumptions", "interactions",
)
_COMPONENTS = ("click", "consumption", "interaction", "growth")
_RESULT_KEYS = (
    "schema_version", "algorithm_version", "operation", "metric", "source",
    "analysis", "limitations",
)
_EXECUTION_ERRORS = frozenset({
    "input_limit", "row_limit", "output_limit", "invalid_request", "invalid_output",
    "timeout", "worker_failed", "resource_limit", "docker_failed", "docker_unavailable",
    "service_busy", "service_unavailable",
})
LIMITATIONS = (
    "来源运行按独立窗口分别分析，不能跨运行累加业务指标或 UV。",
    "指标范围是来源运行的完整上榜新闻，仍不代表全量新闻或全站指标。",
    "冻结案例是经筛选的评测子集，案例覆盖不能作为全榜错误率或运营效果。",
    "同一来源新闻的多个反馈案例仅映射一次指标，不重复计算或补造缺失字段。",
    "质量提示用于核对指标口径，不替代独立标签复核、发布门禁或因果证据。",
)


class SourceRunReader(Protocol):
    """Trusted reader must enforce tenant scope before returning a source run."""

    async def get_run_detail(
        self, *, tenant_id: str, run_id: UUID,
    ) -> HotNewsRunDetailResponse | None: ...


class AnalysisRunnerPort(Protocol):
    """Fixed aggregate execution Port; implementations validate worker output."""

    async def run(self, request: dict) -> dict: ...


class _Unavailable(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _Unavailable("source_window_invalid")
    return value.astimezone(timezone.utc)


def _mapping(case: FrozenEvaluationCase) -> dict:
    return {
        "case_id": case.case_id,
        "feedback_case_id": str(case.feedback_case_id),
        "news_id": case.news_id,
        "label_id": str(case.lineage.label_id),
        "label_version": case.lineage.label_version,
        "case_sha256": case.content_sha256,
    }


class DataLoopDatasetAnalysisService:
    def __init__(
        self, *, source_runs: SourceRunReader, runner: AnalysisRunnerPort,
        max_cases: int = 20,
    ) -> None:
        if type(max_cases) is not int or not 1 <= max_cases <= 100:
            raise ValueError("dataset_analysis_max_cases_invalid")
        self._source_runs = source_runs
        self._runner = runner
        self._max_cases = max_cases

    async def analyze(
        self, *, tenant_id: str, manifest: EvaluationDatasetManifest,
    ) -> dict:
        if not isinstance(tenant_id, str) or not tenant_id.strip() or manifest.tenant_id != tenant_id:
            raise ValueError("dataset_analysis_tenant_mismatch")
        if len(manifest.cases) > self._max_cases:
            raise ValueError("dataset_analysis_case_limit")

        groups: dict[UUID, list[FrozenEvaluationCase]] = {}
        missing = []
        for case in manifest.cases:
            if case.lineage.run_id is None:
                missing.append(case)
            else:
                groups.setdefault(case.lineage.run_id, []).append(case)

        runs = []
        if missing:
            runs.append(self._entry(None, missing, reason="source_run_id_missing"))
        for run_id in sorted(groups, key=str):
            cases = sorted(groups[run_id], key=lambda case: str(case.feedback_case_id))
            entry = self._entry(run_id, cases)
            try:
                self._check_case_group(cases)
                # Query/DB failures deliberately propagate to Activity retry.
                detail = await self._source_runs.get_run_detail(tenant_id=tenant_id, run_id=run_id)
                if detail is None or detail.run.status != "completed":
                    raise _Unavailable("source_completed_run_not_found")
                self._check_source(detail, cases, tenant_id=tenant_id, run_id=run_id)
                projected = project_dataset(detail, run_id=str(run_id), tenant_id=tenant_id)
                request = validate_request({
                    "schema_version": "1.0", "operation": "overview", "metric": "ctr",
                    "dataset": projected,
                })
                projection = request["dataset"]
                projection_hash = sha256(canonical_json_bytes(projection)).hexdigest()
                entry.update(projection=projection, projection_sha256=projection_hash,
                             ranked_news_count=len(projection["rows"]))
                results = {}
                for operation in ("overview", "quality"):
                    operation_request = {**request, "operation": operation,
                                         "dataset": deepcopy(projection)}
                    result = await self._runner.run(operation_request)
                    results[operation] = self._stable_result(
                        result, operation=operation, projection=projection,
                        projection_hash=projection_hash,
                    )
                entry.update(status="completed", reason_code=None, results=results)
            except _Unavailable as exc:
                entry["reason_code"] = exc.code
            except (AnalysisProjectionError, AnalysisInputError, AttributeError, KeyError,
                    TypeError, ValueError, InvalidOperation, OverflowError):
                # Malformed persisted snapshots cannot be rescued by a case-only
                # projection or a truncated ranking.
                entry["reason_code"] = "source_projection_invalid"
            except AnalysisExecutionError as exc:
                code = exc.error_code if exc.error_code in _EXECUTION_ERRORS else "execution_failed"
                entry["reason_code"] = f"analysis_{code}"
            runs.append(entry)

        completed = [run for run in runs if run["status"] == "completed"]
        covered = sum(len(run["case_mapping"]) for run in completed)
        return {
            "schema_version": "1.0", "report_version": DATASET_ANALYSIS_REPORT_VERSION,
            "scope": "referenced_ranked_runs", "tenant_id": tenant_id,
            "dataset_id": str(manifest.dataset_id), "dataset_sha256": manifest.content_sha256,
            "analysis_status": "completed" if covered == len(manifest.cases) else "degraded",
            "summary": {
                "case_count": len(manifest.cases), "source_run_count": len(groups),
                "analyzed_run_count": len(completed), "covered_case_count": covered,
                "unavailable_case_count": len(manifest.cases) - covered,
            },
            "case_statistics": {
                "dataset_layer": manifest.dataset_layer,
                "source_types": self._counts(
                    (case.lineage.source_type for case in manifest.cases), get_args(FeedbackSource)),
                "problem_types": self._counts(
                    (case.lineage.problem_type for case in manifest.cases), get_args(FeedbackProblemType)),
                "severities": self._counts(
                    (case.severity for case in manifest.cases), get_args(FeedbackSeverity)),
                "verdicts": self._counts(case.expected.verdict for case in manifest.cases),
            },
            "source_runs": runs, "limitations": list(LIMITATIONS),
        }

    @staticmethod
    def _counts(values, allowed=None) -> dict:
        # Lineage classification historically allows arbitrary strings. Do not
        # copy free-form case text into the numeric report's category names.
        return dict(sorted(Counter(
            value if allowed is None or value in allowed else "unknown"
            for value in values
        ).items()))

    @staticmethod
    def _entry(run_id, cases, *, reason=None) -> dict:
        return {
            "run_id": str(run_id) if run_id is not None else None,
            "status": "unavailable", "reason_code": reason,
            "case_mapping": [_mapping(case) for case in cases],
            "referenced_unique_news_count": len({case.news_id for case in cases}),
            "ranked_news_count": None, "projection": None,
            "projection_sha256": None, "results": {},
        }

    @staticmethod
    def _check_case_group(cases) -> None:
        first = cases[0]
        identity = (
            first.lineage.run_idempotency_key, first.lineage.production_bundle_version,
            _utc(first.analysis_input.window_start), _utc(first.analysis_input.window_end),
        )
        fingerprints = {}
        for case in cases:
            if identity != (
                case.lineage.run_idempotency_key, case.lineage.production_bundle_version,
                _utc(case.analysis_input.window_start), _utc(case.analysis_input.window_end),
            ):
                raise _Unavailable("source_lineage_conflict")
            payload = case.analysis_input.model_dump(mode="json")
            payload["window_start"] = _utc(case.analysis_input.window_start).isoformat()
            payload["window_end"] = _utc(case.analysis_input.window_end).isoformat()
            fingerprint = sha256(canonical_json_bytes(payload)).hexdigest()
            previous = fingerprints.setdefault(case.news_id, fingerprint)
            if previous != fingerprint:
                raise _Unavailable("case_snapshot_conflict")

    @staticmethod
    def _check_source(detail, cases, *, tenant_id, run_id) -> None:
        first = cases[0]
        source = detail.run
        # The production reader returns a tenant-scoped view without a tenant
        # field. Readers with such a field must also agree with trusted scope.
        for value in (detail, source):
            if getattr(value, "tenant_id", tenant_id) != tenant_id:
                raise _Unavailable("source_tenant_mismatch")
        if source.run_id != run_id or source.idempotency_key != first.lineage.run_idempotency_key:
            raise _Unavailable("source_identity_mismatch")
        if source.production_bundle_version != first.lineage.production_bundle_version:
            raise _Unavailable("source_bundle_mismatch")
        if (_utc(source.window_start), _utc(source.window_end)) != (
            _utc(first.analysis_input.window_start), _utc(first.analysis_input.window_end),
        ):
            raise _Unavailable("source_window_mismatch")
        by_news = {item.news_id: item for item in detail.ranked_news}
        if len(by_news) != len(detail.ranked_news):
            raise _Unavailable("source_projection_invalid")
        for case in cases:
            item = by_news.get(case.news_id)
            if item is None:
                raise _Unavailable("source_news_missing")
            frozen = case.analysis_input
            if item.metrics.content_type != frozen.content_type or any(
                getattr(item.metrics, key) != getattr(frozen.metrics, key) for key in _COUNTS
            ):
                raise _Unavailable("source_metric_mismatch")
            # Frozen model input deliberately uses float() of the authoritative
            # Decimal. Compare using that same conversion, never a broad epsilon.
            if float(Decimal(item.metrics.ctr)) != frozen.metrics.ctr:
                raise _Unavailable("source_metric_mismatch")
            if float(Decimal(item.hot_score.score)) != frozen.hot_score or any(
                float(Decimal(getattr(item.hot_score, f"{key}_component")))
                != getattr(frozen.score_components, key) for key in _COMPONENTS
            ):
                raise _Unavailable("source_hot_score_mismatch")

    @staticmethod
    def _stable_result(result, *, operation, projection, projection_hash) -> dict:
        if not isinstance(result, dict) or any(key not in result for key in _RESULT_KEYS):
            raise _Unavailable("analysis_invalid_output")
        source = {
            "run_id": projection["run_id"], "window_start": projection["window_start"],
            "window_end": projection["window_end"], "bundle_version": projection["bundle_version"],
            "row_count": len(projection["rows"]), "scope": "ranked_news_only",
            "snapshot_sha256": projection_hash,
        }
        if (result["schema_version"] != "1.0" or result["algorithm_version"] != ALGORITHM_VERSION
                or result["operation"] != operation or result["metric"] != "ctr"
                or result["source"] != source or not isinstance(result["analysis"], dict)
                or not isinstance(result["limitations"], list)):
            raise _Unavailable("analysis_invalid_output")
        # Execution timing, backend request IDs and additional runner diagnostics
        # do not belong in the stable content-addressed evidence report.
        return deepcopy({key: result[key] for key in _RESULT_KEYS})
