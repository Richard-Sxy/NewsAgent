"""将已审批 Feedback Case 冻结为内容寻址的评测数据集。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import quote
from uuid import NAMESPACE_URL, UUID, uuid5

import boto3
from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings
from app.domain.errors import ArtifactStoreError
from app.repositories.evaluation_dataset import (
    EvaluationDatasetConflictError,
    EvaluationDatasetRepository,
)
from app.schemas.evaluation_dataset import (
    EvaluationDatasetArtifactReceipt,
    EvaluationDatasetManifest,
    EvaluationDatasetReference,
    FreezeEvaluationDatasetCommand,
    FreezeEvaluationDatasetResult,
    FrozenEvaluationCase,
)


class EvaluationDatasetNotReadyError(ValueError):
    """请求的 Feedback Case 不存在、未审批或不可评测。"""

    retryable = False


class EvaluationDatasetIntegrityError(RuntimeError):
    """Artifact Port 回执与本地规范内容不一致。"""

    retryable = False


class EvaluationDatasetArtifactStore(Protocol):
    """S3/MinIO 风格的不可变 JSON Artifact Port。"""

    async def put_json(
        self,
        *,
        tenant_id: str,
        dataset_id: UUID,
        dataset_name: str,
        dataset_version: str,
        content: bytes,
        content_sha256: str,
    ) -> EvaluationDatasetArtifactReceipt: ...

    async def get_json(
        self,
        *,
        storage_uri: str,
        expected_sha256: str,
    ) -> dict: ...


class S3EvaluationDatasetArtifactStore:
    """使用内容哈希对 S3/MinIO 对象定址的具体适配器。"""

    def __init__(
        self,
        settings: Settings,
        *,
        client: BaseClient | None = None,
    ) -> None:
        self._bucket = settings.artifact_bucket
        self._prefix = settings.artifact_prefix.strip("/")
        self._sse_algorithm = settings.artifact_sse_algorithm
        self._kms_key_id = settings.artifact_kms_key_id
        if self._sse_algorithm not in {"none", "AES256", "aws:kms"}:
            raise ValueError(
                "ARTIFACT_SSE_ALGORITHM only supports none, AES256 or aws:kms"
            )
        if self._sse_algorithm == "aws:kms" and not self._kms_key_id:
            raise ValueError("ARTIFACT_KMS_KEY_ID is required for aws:kms")
        self._client = client or boto3.client(
            "s3",
            endpoint_url=settings.artifact_endpoint,
            region_name=settings.artifact_region,
        )

    async def put_json(
        self,
        *,
        tenant_id: str,
        dataset_id: UUID,
        dataset_name: str,
        dataset_version: str,
        content: bytes,
        content_sha256: str,
    ) -> EvaluationDatasetArtifactReceipt:
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sha256 != content_sha256:
            raise EvaluationDatasetIntegrityError(
                "provided dataset checksum does not match content"
            )
        object_key = self._object_key(
            tenant_id=tenant_id,
            dataset_id=dataset_id,
            dataset_name=dataset_name,
            dataset_version=dataset_version,
            content_sha256=content_sha256,
        )
        await asyncio.to_thread(
            self._put_object,
            object_key,
            content,
            content_sha256,
        )
        return EvaluationDatasetArtifactReceipt(
            storage_uri=f"s3://{self._bucket}/{object_key}",
            content_sha256=content_sha256,
            content_size=len(content),
        )

    async def get_json(
        self,
        *,
        storage_uri: str,
        expected_sha256: str,
    ) -> dict:
        object_key = self._parse_storage_uri(storage_uri)
        try:
            response = await asyncio.to_thread(
                self._client.get_object,
                Bucket=self._bucket,
                Key=object_key,
            )
            content = await asyncio.to_thread(response["Body"].read)
        except (BotoCoreError, ClientError, KeyError) as exc:
            raise ArtifactStoreError(
                f"failed to read evaluation dataset: {storage_uri}"
            ) from exc
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sha256 != expected_sha256:
            raise EvaluationDatasetIntegrityError(
                "evaluation dataset artifact checksum mismatch"
            )
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvaluationDatasetIntegrityError(
                "evaluation dataset artifact is not valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise EvaluationDatasetIntegrityError(
                "evaluation dataset artifact root must be an object"
            )
        return payload

    def _put_object(
        self,
        object_key: str,
        content: bytes,
        content_sha256: str,
    ) -> None:
        request = {
            "Bucket": self._bucket,
            "Key": object_key,
            "Body": content,
            "ContentType": "application/json",
            "ChecksumSHA256": base64.b64encode(
                bytes.fromhex(content_sha256)
            ).decode("ascii"),
            "Metadata": {
                "content-sha256": content_sha256,
                "artifact-kind": "evaluation-dataset",
            },
        }
        if self._sse_algorithm != "none":
            request["ServerSideEncryption"] = self._sse_algorithm
        if self._sse_algorithm == "aws:kms":
            request["SSEKMSKeyId"] = self._kms_key_id
        try:
            self._client.put_object(**request)
        except (BotoCoreError, ClientError) as exc:
            raise ArtifactStoreError(
                f"failed to write evaluation dataset: "
                f"s3://{self._bucket}/{object_key}"
            ) from exc

    def _object_key(
        self,
        *,
        tenant_id: str,
        dataset_id: UUID,
        dataset_name: str,
        dataset_version: str,
        content_sha256: str,
    ) -> str:
        parts = [
            self._prefix,
            "tenants",
            quote(tenant_id, safe=""),
            "evaluation-datasets",
            quote(dataset_name, safe=""),
            quote(dataset_version, safe=""),
            str(dataset_id),
            f"{content_sha256}.json",
        ]
        return "/".join(part for part in parts if part)

    def _parse_storage_uri(self, storage_uri: str) -> str:
        uri_prefix = f"s3://{self._bucket}/"
        if not storage_uri.startswith(uri_prefix):
            raise ArtifactStoreError(
                "evaluation dataset URI does not belong to configured bucket"
            )
        object_key = storage_uri[len(uri_prefix) :]
        if not object_key or ".." in object_key.split("/"):
            raise ArtifactStoreError("evaluation dataset object key is invalid")
        required_prefix = f"{self._prefix}/" if self._prefix else ""
        if required_prefix and not object_key.startswith(required_prefix):
            raise ArtifactStoreError(
                "evaluation dataset URI does not belong to service prefix"
            )
        return object_key


class EvaluationDatasetFreezer:
    """
    Data Loop 冻结服务。

    Artifact 采用内容寻址，因此幂等重放不会改变已冻结对象。
    PostgreSQL Repository 负责在同一事务内写入 Dataset/Case 索引并将
    Feedback Case 状态从 ``labeled`` 推进到 ``frozen``。
    """

    def __init__(
        self,
        *,
        repository: EvaluationDatasetRepository,
        artifact_store: EvaluationDatasetArtifactStore,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[FreezeEvaluationDatasetCommand], UUID] | None = None,
    ) -> None:
        self._repository = repository
        self._artifact_store = artifact_store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or self._stable_dataset_id

    async def freeze(
        self,
        command: FreezeEvaluationDatasetCommand,
    ) -> FreezeEvaluationDatasetResult:
        existing = await self._repository.get_by_idempotency_key(
            tenant_id=command.tenant_id,
            idempotency_key=command.idempotency_key,
        )
        if existing is not None:
            if existing.request_fingerprint != command.request_fingerprint:
                raise EvaluationDatasetConflictError(
                    "idempotency key was reused with different freeze content"
                )
            return FreezeEvaluationDatasetResult(
                dataset=existing,
                created=False,
            )
        occupied_version = await self._repository.get_by_name_version(
            tenant_id=command.tenant_id,
            dataset_name=command.dataset_name,
            dataset_version=command.dataset_version,
        )
        if occupied_version is not None:
            raise EvaluationDatasetConflictError(
                "dataset name/version is already frozen under another request"
            )

        frozen_at = self._clock()
        if frozen_at.tzinfo is None or frozen_at.utcoffset() is None:
            raise ValueError("freezer clock must return an aware datetime")
        if command.source_cutoff_at > frozen_at:
            raise EvaluationDatasetNotReadyError(
                "source_cutoff_at cannot be in the future"
            )

        snapshots = await self._repository.list_approved_feedback_snapshots(
            tenant_id=command.tenant_id,
            feedback_case_ids=command.feedback_case_ids,
            source_cutoff_at=command.source_cutoff_at,
        )
        requested_ids = set(command.feedback_case_ids)
        actual_ids = {snapshot.feedback_case_id for snapshot in snapshots}
        if requested_ids != actual_ids:
            unavailable = sorted(requested_ids - actual_ids, key=str)
            raise EvaluationDatasetNotReadyError(
                "feedback cases are missing, cross-tenant, not labeled, "
                f"or not approved: {[str(item) for item in unavailable]}"
            )

        cases: list[FrozenEvaluationCase] = []
        for snapshot in sorted(snapshots, key=lambda item: str(item.feedback_case_id)):
            if snapshot.tenant_id != command.tenant_id:
                raise EvaluationDatasetNotReadyError(
                    "repository returned a cross-tenant feedback case"
                )
            if snapshot.lineage.recorded_at > command.source_cutoff_at:
                raise EvaluationDatasetNotReadyError(
                    f"feedback case {snapshot.feedback_case_id} is newer than cutoff"
                )
            if snapshot.lineage.label_approved_at > frozen_at:
                raise EvaluationDatasetNotReadyError(
                    f"feedback case {snapshot.feedback_case_id} has a future label"
                )
            cases.append(
                FrozenEvaluationCase(
                    case_id=f"feedback:{snapshot.feedback_case_id}",
                    feedback_case_id=snapshot.feedback_case_id,
                    news_id=snapshot.news_id,
                    layer=command.dataset_layer,
                    severity=snapshot.severity,
                    analysis_input=snapshot.analysis_input_snapshot,
                    observed_output=snapshot.analysis_output_snapshot,
                    expected=snapshot.expected,
                    lineage=snapshot.lineage,
                )
            )

        manifest = EvaluationDatasetManifest(
            dataset_id=self._id_factory(command),
            tenant_id=command.tenant_id,
            dataset_name=command.dataset_name,
            dataset_version=command.dataset_version,
            dataset_layer=command.dataset_layer,
            description=command.description,
            source_cutoff_at=command.source_cutoff_at,
            frozen_at=frozen_at,
            frozen_by=command.frozen_by,
            cases=tuple(cases),
        )
        content = manifest.canonical_content
        content_sha256 = hashlib.sha256(content).hexdigest()
        artifact = await self._artifact_store.put_json(
            tenant_id=command.tenant_id,
            dataset_id=manifest.dataset_id,
            dataset_name=command.dataset_name,
            dataset_version=command.dataset_version,
            content=content,
            content_sha256=content_sha256,
        )
        if (
            artifact.content_sha256 != content_sha256
            or artifact.content_size != len(content)
        ):
            raise EvaluationDatasetIntegrityError(
                "artifact receipt does not match canonical dataset content"
            )

        reference, created = await self._repository.save_frozen_dataset(
            manifest=manifest,
            artifact=artifact,
            idempotency_key=command.idempotency_key,
            request_fingerprint=command.request_fingerprint,
        )
        return FreezeEvaluationDatasetResult(
            dataset=reference,
            created=created,
        )

    async def load_manifest(
        self,
        *,
        tenant_id: str,
        dataset_id: UUID,
    ) -> EvaluationDatasetManifest:
        """按租户读取并校验 Artifact，供 Offline Replay 使用。"""

        reference = await self._repository.get_by_id(
            tenant_id=tenant_id,
            dataset_id=dataset_id,
        )
        if reference is None:
            raise EvaluationDatasetNotReadyError("evaluation dataset not found")
        if reference.tenant_id != tenant_id:
            # Repository 必须租户隔离；这里再做一次防御性校验，
            # 且不读取他租户的 Artifact。
            raise EvaluationDatasetNotReadyError("evaluation dataset not found")
        manifest = await self._load_reference(reference)
        indexes = await self._repository.list_case_indexes(
            tenant_id=tenant_id,
            dataset_id=dataset_id,
        )
        if len(indexes) != len(manifest.cases):
            raise EvaluationDatasetIntegrityError(
                "dataset case index count differs from manifest"
            )
        for position, (index, case) in enumerate(zip(indexes, manifest.cases)):
            if (
                index.tenant_id != tenant_id
                or index.dataset_id != dataset_id
                or index.position != position
                or index.feedback_case_id != case.feedback_case_id
                or index.case_id != case.case_id
                or index.news_id != case.news_id
                or index.dataset_layer != case.layer
                or index.label_version != case.lineage.label_version
                or index.case_content_sha256 != case.content_sha256
            ):
                raise EvaluationDatasetIntegrityError(
                    f"dataset case index differs from manifest at position {position}"
                )
        return manifest

    async def _load_reference(
        self,
        reference: EvaluationDatasetReference,
    ) -> EvaluationDatasetManifest:
        payload = await self._artifact_store.get_json(
            storage_uri=reference.artifact_uri,
            expected_sha256=reference.content_sha256,
        )
        try:
            manifest = EvaluationDatasetManifest.model_validate(payload)
        except ValueError as exc:
            raise EvaluationDatasetIntegrityError(
                "evaluation dataset manifest failed schema validation"
            ) from exc
        if manifest.content_sha256 != reference.content_sha256:
            raise EvaluationDatasetIntegrityError(
                "canonical manifest checksum differs from database reference"
            )
        if (
            manifest.dataset_id != reference.dataset_id
            or manifest.tenant_id != reference.tenant_id
            or manifest.dataset_name != reference.dataset_name
            or manifest.dataset_version != reference.dataset_version
            or manifest.dataset_layer != reference.dataset_layer
            or manifest.status != reference.status
            or len(manifest.cases) != reference.case_count
        ):
            raise EvaluationDatasetIntegrityError(
                "manifest identity differs from database reference"
            )
        return manifest

    @staticmethod
    def _stable_dataset_id(command: FreezeEvaluationDatasetCommand) -> UUID:
        identity = (
            "newsagent:evaluation-dataset:"
            f"{command.tenant_id}:{command.dataset_name}:"
            f"{command.dataset_version}"
        )
        return uuid5(NAMESPACE_URL, identity)


def to_hot_news_evaluation_dataset(manifest: EvaluationDatasetManifest):
    """将冻结 Manifest 转为现有确定性热点评测器的输入。"""

    # 延迟导入避免 Schema 层反向依赖评测执行器。
    from app.evaluation.hot_news import (
        ExpectedHotNewsAnalysis,
        HotNewsEvaluationCase,
        HotNewsEvaluationDataset,
    )

    cases = tuple(
        HotNewsEvaluationCase(
            case_id=case.case_id,
            description=case.expected.operator_comment,
            tags=(
                manifest.dataset_layer,
                f"severity:{case.severity}",
                f"source:{case.lineage.source_type}",
                f"problem:{case.lineage.problem_type}",
            ),
            analysis_input=case.analysis_input,
            expected=ExpectedHotNewsAnalysis(
                allowed_dominant_drivers=(
                    case.expected.allowed_dominant_drivers
                ),
                required_evidence_news_ids=(
                    case.expected.required_evidence_news_ids
                ),
                forbidden_evidence_news_ids=(
                    case.expected.forbidden_evidence_news_ids
                ),
                required_metric_keys=case.expected.required_metric_keys,
                must_state_limitation=case.expected.must_state_limitation,
            ),
        )
        for case in manifest.cases
    )
    return HotNewsEvaluationDataset(
        dataset_name=manifest.dataset_name,
        dataset_version=manifest.dataset_version,
        schema_version=manifest.schema_version,
        description=manifest.description,
        cases=cases,
    )
