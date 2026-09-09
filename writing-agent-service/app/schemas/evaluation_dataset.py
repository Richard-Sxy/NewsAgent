"""Data Loop 不可变评测数据集契约。

该模块只保存回放必需的聚合输入、模型输出和人工标签，
不允许企业原始用户行为明细进入数据集。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from app.schemas.hot_news import (
    DriverType,
    HotNewsAnalysisInput,
    MetricKey,
)


EvaluationDatasetLayer = Literal[
    "golden",
    "fresh_bad_case",
    "high_risk_regression",
]
FeedbackVerdict = Literal[
    "correct",
    "incorrect",
    "partially_correct",
    "not_evaluable",
]


def canonical_json_bytes(value: BaseModel | dict[str, Any]) -> bytes:
    """输出跨进程稳定的 UTF-8 JSON。

    ``sort_keys`` 和紧凑分隔符是数据集哈希契约的一部分；
    ``allow_nan=False`` 避免产生非标准 JSON。
    """

    payload = (
        value.model_dump(mode="json", by_alias=True, exclude_none=False)
        if isinstance(value, BaseModel)
        else value
    )
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_sha256(value: BaseModel | dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class EvaluationExpectedLabel(BaseModel):
    """经过人工审批的最小可确定评测标签。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: FeedbackVerdict
    allowed_dominant_drivers: tuple[DriverType, ...] = Field(min_length=1)
    required_evidence_news_ids: tuple[str, ...] = ()
    forbidden_evidence_news_ids: tuple[str, ...] = ()
    required_metric_keys: tuple[MetricKey, ...] = ()
    must_state_limitation: bool = False
    operator_comment: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_label(self) -> "EvaluationExpectedLabel":
        if self.verdict == "not_evaluable":
            raise ValueError(
                "not_evaluable feedback cannot enter an evaluation dataset"
            )
        if len(self.allowed_dominant_drivers) != len(
            set(self.allowed_dominant_drivers)
        ):
            raise ValueError("allowed_dominant_drivers cannot contain duplicates")
        if len(self.required_metric_keys) != len(set(self.required_metric_keys)):
            raise ValueError("required_metric_keys cannot contain duplicates")

        required = set(self.required_evidence_news_ids)
        forbidden = set(self.forbidden_evidence_news_ids)
        if required & forbidden:
            raise ValueError("required and forbidden evidence cannot overlap")
        if any(not item.strip() for item in required | forbidden):
            raise ValueError("evidence news_ids cannot be empty")
        return self


class EvaluationSourceLineage(BaseModel):
    """从线上 Feedback Case 到冻结 Case 的可追溯血缘。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    feedback_case_id: UUID
    feedback_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: UUID | None = None
    run_idempotency_key: str = Field(min_length=1, max_length=160)
    source_type: str = Field(min_length=1, max_length=64)
    problem_type: str = Field(min_length=1, max_length=64)
    source_reference: dict[str, Any] = Field(default_factory=dict)
    production_bundle_version: str = Field(min_length=1, max_length=120)
    occurred_at: AwareDatetime
    recorded_at: AwareDatetime
    label_id: UUID
    label_version: int = Field(ge=1)
    label_approved_by: str = Field(min_length=1, max_length=128)
    label_approved_at: AwareDatetime

    @model_validator(mode="after")
    def validate_time_order(self) -> "EvaluationSourceLineage":
        if self.recorded_at < self.occurred_at:
            raise ValueError("recorded_at cannot precede occurred_at")
        if self.label_approved_at < self.recorded_at:
            raise ValueError("label approval cannot precede feedback recording")
        return self


class ApprovedFeedbackSnapshot(BaseModel):
    """Repository 向 Freezer 提供的已审批、可冻结快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=128)
    feedback_case_id: UUID
    news_id: str = Field(min_length=1, max_length=128)
    severity: str = Field(min_length=1, max_length=32)
    analysis_input_snapshot: HotNewsAnalysisInput
    analysis_output_snapshot: dict[str, Any] | None = None
    expected: EvaluationExpectedLabel
    lineage: EvaluationSourceLineage

    @model_validator(mode="after")
    def validate_identity_and_evidence(self) -> "ApprovedFeedbackSnapshot":
        if self.feedback_case_id != self.lineage.feedback_case_id:
            raise ValueError("feedback_case_id does not match lineage")
        if self.news_id != self.analysis_input_snapshot.news_id:
            raise ValueError("news_id does not match analysis input")

        available = {
            evidence.news_id
            for evidence in self.analysis_input_snapshot.related_news
        }
        missing = set(self.expected.required_evidence_news_ids) - available
        if missing:
            raise ValueError(
                "required evidence is absent from analysis input: "
                f"{sorted(missing)}"
            )
        return self


class FrozenEvaluationCase(BaseModel):
    """冻结数据集中的单个可回放 Case。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1, max_length=160)
    feedback_case_id: UUID
    news_id: str = Field(min_length=1, max_length=128)
    layer: EvaluationDatasetLayer
    severity: str = Field(min_length=1, max_length=32)
    analysis_input: HotNewsAnalysisInput
    observed_output: dict[str, Any] | None = None
    expected: EvaluationExpectedLabel
    lineage: EvaluationSourceLineage

    @model_validator(mode="after")
    def validate_case(self) -> "FrozenEvaluationCase":
        if self.case_id != f"feedback:{self.feedback_case_id}":
            raise ValueError("case_id must be derived from feedback_case_id")
        if self.news_id != self.analysis_input.news_id:
            raise ValueError("news_id does not match analysis input")
        if self.feedback_case_id != self.lineage.feedback_case_id:
            raise ValueError("feedback_case_id does not match lineage")
        available = {item.news_id for item in self.analysis_input.related_news}
        missing = set(self.expected.required_evidence_news_ids) - available
        if missing:
            raise ValueError(
                "required evidence is absent from frozen input: "
                f"{sorted(missing)}"
            )
        return self

    @property
    def content_sha256(self) -> str:
        return canonical_json_sha256(self)


class EvaluationDatasetManifest(BaseModel):
    """存入 S3/MinIO 的完整、不可变数据集。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: UUID
    tenant_id: str = Field(min_length=1, max_length=128)
    dataset_name: str = Field(min_length=1, max_length=128)
    dataset_version: str = Field(min_length=1, max_length=64)
    dataset_layer: EvaluationDatasetLayer
    status: Literal["frozen"] = "frozen"
    schema_version: Literal["1.0"] = "1.0"
    description: str = Field(min_length=1, max_length=1000)
    source_cutoff_at: AwareDatetime
    frozen_at: AwareDatetime
    frozen_by: str = Field(min_length=1, max_length=128)
    cases: tuple[FrozenEvaluationCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_manifest(self) -> "EvaluationDatasetManifest":
        feedback_ids = [case.feedback_case_id for case in self.cases]
        if len(feedback_ids) != len(set(feedback_ids)):
            raise ValueError("feedback cases cannot be duplicated")
        if any(case.layer != self.dataset_layer for case in self.cases):
            raise ValueError("all cases must match the dataset layer")
        if tuple(feedback_ids) != tuple(sorted(feedback_ids, key=str)):
            raise ValueError("cases must use canonical feedback_case_id ordering")
        if self.frozen_at < self.source_cutoff_at:
            raise ValueError("frozen_at cannot precede source_cutoff_at")
        if any(case.lineage.recorded_at > self.source_cutoff_at for case in self.cases):
            raise ValueError("dataset includes feedback recorded after source cutoff")
        if any(
            case.lineage.label_approved_at > self.frozen_at
            for case in self.cases
        ):
            raise ValueError("dataset includes a label approved after freeze time")
        return self

    @property
    def content_sha256(self) -> str:
        return canonical_json_sha256(self)

    @property
    def canonical_content(self) -> bytes:
        return canonical_json_bytes(self)


class FreezeEvaluationDatasetCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=128)
    dataset_name: str = Field(min_length=1, max_length=128)
    dataset_version: str = Field(min_length=1, max_length=64)
    dataset_layer: EvaluationDatasetLayer
    description: str = Field(min_length=1, max_length=1000)
    feedback_case_ids: tuple[UUID, ...] = Field(min_length=1)
    source_cutoff_at: AwareDatetime
    frozen_by: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_feedback_ids(self) -> "FreezeEvaluationDatasetCommand":
        if len(self.feedback_case_ids) != len(set(self.feedback_case_ids)):
            raise ValueError("feedback_case_ids cannot contain duplicates")
        return self

    @property
    def request_fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude={"idempotency_key"})
        payload["feedback_case_ids"] = sorted(payload["feedback_case_ids"])
        return canonical_json_sha256(payload)


class EvaluationDatasetReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: UUID
    tenant_id: str
    dataset_name: str
    dataset_version: str
    dataset_layer: EvaluationDatasetLayer
    status: Literal["frozen"]
    schema_version: str
    description: str
    source_cutoff_at: AwareDatetime
    frozen_at: AwareDatetime
    frozen_by: str
    idempotency_key: str
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_uri: str = Field(min_length=1, max_length=2048)
    artifact_size: int = Field(ge=1)
    case_count: int = Field(ge=1)


class EvaluationDatasetArtifactReceipt(BaseModel):
    """Artifact Port 在完成内容寻址写入后返回的回执。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    storage_uri: str = Field(min_length=1, max_length=2048)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_size: int = Field(ge=1)


class EvaluationDatasetCaseIndex(BaseModel):
    """PostgreSQL 中用于查询血缘的 Case 索引。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: UUID
    tenant_id: str
    feedback_case_id: UUID
    position: int = Field(ge=0)
    case_id: str
    news_id: str
    dataset_layer: EvaluationDatasetLayer
    severity: str
    label_version: int = Field(ge=1)
    case_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FreezeEvaluationDatasetResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset: EvaluationDatasetReference
    created: bool


def utc_now() -> datetime:
    """仅为不具备时钟注入能力的调用方提供标准 UTC 时间。"""

    from datetime import UTC

    return datetime.now(UTC)
