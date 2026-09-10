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


DataLoopArtifactKind = Literal["error-attribution", "candidate-evaluation"]


@dataclass(frozen=True, slots=True)
class DataLoopArtifactReceipt:
    storage_uri: str
    content_sha256: str
    content_size: int


class DataLoopArtifactStore(Protocol):
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
        except (BotoCoreError, ClientError, KeyError) as exc:
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
                "artifact-kind": "data-loop",
            },
        }
        if self._sse != "none":
            request["ServerSideEncryption"] = self._sse
        if self._sse == "aws:kms":
            request["SSEKMSKeyId"] = self._kms_key
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
