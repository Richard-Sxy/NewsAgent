"""Content-addressed S3 artifacts for attribution and offline evaluation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import quote

import boto3
from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings
from app.domain.errors import ArtifactIntegrityError, ArtifactStoreError


DataLoopArtifactKind = Literal[
    "error-attribution", "candidate-evaluation", "dataset-analysis"
]


@dataclass(frozen=True, slots=True)
class DataLoopArtifactReceipt:
    storage_uri: str
    content_sha256: str
    content_size: int


class DataLoopArtifactStore(Protocol):
    async def find_json(
        self,
        *,
        tenant_id: str,
        artifact_kind: DataLoopArtifactKind,
        logical_id: str,
    ) -> tuple[DataLoopArtifactReceipt, dict[str, Any]] | None: ...

    async def put_json_once(
        self,
        *,
        tenant_id: str,
        artifact_kind: DataLoopArtifactKind,
        logical_id: str,
        payload: dict[str, Any],
    ) -> tuple[DataLoopArtifactReceipt, dict[str, Any]]: ...

    async def put_json(
        self,
        *,
        tenant_id: str,
        artifact_kind: DataLoopArtifactKind,
        logical_id: str,
        payload: dict[str, Any],
    ) -> DataLoopArtifactReceipt: ...

    async def get_json(
        self,
        *,
        storage_uri: str,
        expected_sha256: str,
    ) -> dict[str, Any]: ...


class S3DataLoopArtifactStore:
    def __init__(
        self,
        settings: Settings,
        *,
        client: BaseClient | None = None,
    ) -> None:
        self._bucket = settings.artifact_bucket
        self._prefix = settings.artifact_prefix.strip("/")
        self._sse = settings.artifact_sse_algorithm
        self._kms_key = settings.artifact_kms_key_id
        if self._sse not in {"none", "AES256", "aws:kms"}:
            raise ValueError("invalid artifact SSE algorithm")
        if self._sse == "aws:kms" and not self._kms_key:
            raise ValueError("ARTIFACT_KMS_KEY_ID is required for aws:kms")
        self._client = client or boto3.client(
            "s3",
            endpoint_url=settings.artifact_endpoint,
            region_name=settings.artifact_region,
        )

    async def find_json(
        self,
        *,
        tenant_id: str,
        artifact_kind: DataLoopArtifactKind,
        logical_id: str,
    ) -> tuple[DataLoopArtifactReceipt, dict[str, Any]] | None:
        logical_prefix = self._logical_prefix(
            tenant_id=tenant_id,
            artifact_kind=artifact_kind,
            logical_id=logical_id,
        )
        key = f"{logical_prefix}/receipt.json"
        try:
            response = await asyncio.to_thread(
                self._client.get_object, Bucket=self._bucket, Key=key
            )
            content = await asyncio.to_thread(response["Body"].read)
        except ClientError as exc:
            error = (exc.response or {}).get("Error") or {}
            if str(error.get("Code", "")) in {"NoSuchKey", "NotFound", "404"}:
                return None
            raise ArtifactStoreError("failed to read Data Loop artifact receipt") from exc
        except (BotoCoreError, KeyError, OSError) as exc:
            raise ArtifactStoreError("failed to read Data Loop artifact receipt") from exc

        digest = hashlib.sha256(content).hexdigest()
        metadata = response.get("Metadata")
        if not isinstance(metadata, dict):
            raise ArtifactIntegrityError("Data Loop artifact receipt is missing integrity metadata")
        normalized_metadata = {
            str(name).lower(): str(value) for name, value in metadata.items()
        }
        if (
            normalized_metadata.get("content-sha256", "").lower() != digest
            or normalized_metadata.get("artifact-kind") != "data-loop-receipt"
        ):
            raise ArtifactIntegrityError("Data Loop artifact receipt integrity metadata mismatch")
        try:
            pointer = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactIntegrityError("Data Loop artifact receipt is not valid JSON") from exc
        if not isinstance(pointer, dict) or set(pointer) != {
            "schema_version", "tenant_id", "artifact_kind", "logical_id",
            "storage_uri", "content_sha256", "content_size",
        }:
            raise ArtifactIntegrityError("invalid Data Loop artifact receipt shape")
        report_digest = pointer["content_sha256"]
        size = pointer["content_size"]
        if (
            pointer["schema_version"] != "1.0"
            or pointer["tenant_id"] != tenant_id
            or pointer["artifact_kind"] != artifact_kind
            or pointer["logical_id"] != logical_id
            or not isinstance(report_digest, str)
            or len(report_digest) != 64
            or any(char not in "0123456789abcdef" for char in report_digest)
            or type(size) is not int
            or size <= 0
            or pointer["storage_uri"]
            != f"s3://{self._bucket}/{logical_prefix}/{report_digest}.json"
        ):
            raise ArtifactIntegrityError("Data Loop artifact receipt identity or path mismatch")
        receipt = DataLoopArtifactReceipt(
            storage_uri=pointer["storage_uri"],
            content_sha256=report_digest,
            content_size=size,
        )
        payload = await self.get_json(
            storage_uri=receipt.storage_uri,
            expected_sha256=receipt.content_sha256,
        )
        canonical = self._canonical_bytes(payload)
        if (
            len(canonical) != receipt.content_size
            or hashlib.sha256(canonical).hexdigest() != receipt.content_sha256
        ):
            raise ArtifactIntegrityError("Data Loop artifact receipt report size or canonical hash mismatch")
        return receipt, payload

    async def put_json_once(
        self,
        *,
        tenant_id: str,
        artifact_kind: DataLoopArtifactKind,
        logical_id: str,
        payload: dict[str, Any],
    ) -> tuple[DataLoopArtifactReceipt, dict[str, Any]]:
        existing = await self.find_json(
            tenant_id=tenant_id, artifact_kind=artifact_kind, logical_id=logical_id
        )
        if existing is not None:
            return existing
        if not isinstance(payload, dict):
            raise ArtifactStoreError("Data Loop artifact root must be an object")
        # Snapshot the caller's payload before awaiting storage operations.
        frozen_payload = json.loads(self._canonical_bytes(payload))
        receipt = await self.put_json(
            tenant_id=tenant_id, artifact_kind=artifact_kind,
            logical_id=logical_id, payload=frozen_payload,
        )
        logical_prefix = self._logical_prefix(
            tenant_id=tenant_id, artifact_kind=artifact_kind, logical_id=logical_id
        )
        pointer = {
            "schema_version": "1.0",
            "tenant_id": tenant_id,
            "artifact_kind": artifact_kind,
            "logical_id": logical_id,
            "storage_uri": receipt.storage_uri,
            "content_sha256": receipt.content_sha256,
            "content_size": receipt.content_size,
        }
        content = self._canonical_bytes(pointer)
        key = f"{logical_prefix}/receipt.json"
        request = self._write_request(
            key=key, content=content,
            digest=hashlib.sha256(content).hexdigest(),
            metadata_kind="data-loop-receipt",
        )
        try:
            await asyncio.to_thread(self._client.put_object, **request)
        except ClientError as exc:
            if not self._is_conditional_write_conflict(exc):
                raise ArtifactStoreError("failed to write Data Loop artifact receipt") from exc
            winner = await self.find_json(
                tenant_id=tenant_id, artifact_kind=artifact_kind, logical_id=logical_id
            )
            if winner is None:
                raise ArtifactStoreError("Data Loop artifact receipt conflict has no readable winner") from exc
            return winner
        except BotoCoreError as exc:
            raise ArtifactStoreError("failed to write Data Loop artifact receipt") from exc
        return receipt, frozen_payload

    async def put_json(
        self,
        *,
        tenant_id: str,
        artifact_kind: DataLoopArtifactKind,
        logical_id: str,
        payload: dict[str, Any],
    ) -> DataLoopArtifactReceipt:
        content = self._canonical_bytes(payload)
        digest = hashlib.sha256(content).hexdigest()
        key = "/".join(
            part
            for part in (
                self._prefix,
                "tenants",
                quote(tenant_id, safe=""),
                "data-loop",
                artifact_kind,
                quote(logical_id, safe=""),
                f"{digest}.json",
            )
            if part
        )
        await asyncio.to_thread(self._put_object, key, content, digest)
        return DataLoopArtifactReceipt(
            storage_uri=f"s3://{self._bucket}/{key}",
            content_sha256=digest,
            content_size=len(content),
        )

    async def get_json(
        self,
        *,
        storage_uri: str,
        expected_sha256: str,
    ) -> dict[str, Any]:
        key = self._parse_uri(storage_uri)
        try:
            response = await asyncio.to_thread(
                self._client.get_object,
                Bucket=self._bucket,
                Key=key,
            )
            content = await asyncio.to_thread(response["Body"].read)
        except (BotoCoreError, ClientError, KeyError, OSError) as exc:
            raise ArtifactStoreError(
                f"failed to read Data Loop artifact: {storage_uri}"
            ) from exc
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected_sha256:
            raise ArtifactIntegrityError(
                "Data Loop artifact checksum mismatch"
            )
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactIntegrityError(
                "Data Loop artifact is not valid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise ArtifactIntegrityError(
                "Data Loop artifact root must be an object"
            )
        return value

    def _put_object(self, key: str, content: bytes, digest: str) -> None:
        request = self._write_request(
            key=key, content=content, digest=digest, metadata_kind="data-loop"
        )
        try:
            self._client.put_object(**request)
        except ClientError as exc:
            if self._is_conditional_write_conflict(exc):
                self._verify_existing_object(
                    key=key,
                    expected_content=content,
                    expected_sha256=digest,
                )
                return
            raise ArtifactStoreError(
                f"failed to write Data Loop artifact: s3://{self._bucket}/{key}"
            ) from exc
        except BotoCoreError as exc:
            raise ArtifactStoreError(
                f"failed to write Data Loop artifact: s3://{self._bucket}/{key}"
            ) from exc

    def _write_request(
        self, *, key: str, content: bytes, digest: str, metadata_kind: str
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": content,
            "IfNoneMatch": "*",
            "ContentType": "application/json",
            "ChecksumSHA256": base64.b64encode(
                bytes.fromhex(digest)
            ).decode("ascii"),
            "Metadata": {
                "content-sha256": digest,
                "artifact-kind": metadata_kind,
            },
        }
        if self._sse != "none":
            request["ServerSideEncryption"] = self._sse
        if self._sse == "aws:kms":
            request["SSEKMSKeyId"] = self._kms_key
        return request

    def _logical_prefix(
        self, *, tenant_id: str, artifact_kind: DataLoopArtifactKind, logical_id: str
    ) -> str:
        if (
            not isinstance(tenant_id, str) or not tenant_id.strip() or tenant_id in {".", ".."}
            or not isinstance(logical_id, str) or not logical_id.strip() or logical_id in {".", ".."}
            or artifact_kind not in {"error-attribution", "candidate-evaluation", "dataset-analysis"}
        ):
            raise ArtifactStoreError("invalid Data Loop artifact identity")
        return "/".join(
            part for part in (
                self._prefix, "tenants", quote(tenant_id, safe=""),
                "data-loop", artifact_kind, quote(logical_id, safe=""),
            ) if part
        )

    def _verify_existing_object(
        self,
        *,
        key: str,
        expected_content: bytes,
        expected_sha256: str,
    ) -> None:
        storage_uri = f"s3://{self._bucket}/{key}"
        try:
            response = self._client.get_object(
                Bucket=self._bucket,
                Key=key,
            )
            existing_content = response["Body"].read()
        except (BotoCoreError, ClientError, KeyError, OSError) as exc:
            raise ArtifactStoreError(
                "failed to verify an existing Data Loop artifact after "
                f"conditional-write conflict: {storage_uri}"
            ) from exc

        actual_sha256 = hashlib.sha256(existing_content).hexdigest()
        if (
            existing_content != expected_content
            or actual_sha256 != expected_sha256
        ):
            raise ArtifactIntegrityError(
                "existing Data Loop artifact content does not match its "
                "content-addressed key"
            )

        metadata = response.get("Metadata")
        if not isinstance(metadata, dict):
            raise ArtifactIntegrityError(
                "existing Data Loop artifact is missing integrity metadata"
            )
        normalized_metadata = {
            str(metadata_key).lower(): str(value)
            for metadata_key, value in metadata.items()
        }
        if (
            normalized_metadata.get("content-sha256", "").lower()
            != expected_sha256
            or normalized_metadata.get("artifact-kind") != "data-loop"
        ):
            raise ArtifactIntegrityError(
                "existing Data Loop artifact integrity metadata does not "
                "match its content-addressed key"
            )

    @staticmethod
    def _is_conditional_write_conflict(exc: ClientError) -> bool:
        response = exc.response or {}
        error = response.get("Error") or {}
        code = str(error.get("Code", ""))
        response_metadata = response.get("ResponseMetadata") or {}
        status = response_metadata.get("HTTPStatusCode")
        return code in {
            "409",
            "412",
            "Conflict",
            "ConditionalRequestConflict",
            "PreconditionFailed",
        } or status in {409, 412}

    def _parse_uri(self, storage_uri: str) -> str:
        prefix = f"s3://{self._bucket}/"
        if not storage_uri.startswith(prefix):
            raise ArtifactStoreError(
                "Data Loop artifact URI is outside the configured bucket"
            )
        key = storage_uri[len(prefix) :]
        required = f"{self._prefix}/" if self._prefix else ""
        if (
            not key
            or ".." in key.split("/")
            or (required and not key.startswith(required))
        ):
            raise ArtifactStoreError("invalid Data Loop artifact URI")
        return key

    @staticmethod
    def _canonical_bytes(payload: dict[str, Any]) -> bytes:
        try:
            return json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ArtifactStoreError(
                "Data Loop artifact cannot be serialized"
            ) from exc
