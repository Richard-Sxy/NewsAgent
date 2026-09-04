import asyncio
import base64
import hashlib
import json
import re
import uuid
from typing import Any

import boto3
from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings
from app.domain.errors import ArtifactIntegrityError, ArtifactStoreError
from app.domain.execution import ArtifactType
from app.schemas.checkpoint import ArtifactDescriptor


class S3ArtifactStore:
    """将已校验 Agent 输出保存为内容寻址的不可变 JSON 对象。"""

    def __init__(
        self,
        settings: Settings,
        *,
        client: BaseClient | None = None,
    ) -> None:
        self.bucket = settings.artifact_bucket
        self.prefix = settings.artifact_prefix.strip("/")
        self.sse_algorithm = settings.artifact_sse_algorithm
        self.kms_key_id = settings.artifact_kms_key_id
        
        if self.sse_algorithm not in {"none", "AES256", "aws:kms"}:
            raise ValueError(
                "ARTIFACT_SSE_ALGORITHM 只支持 none、AES256 或 aws:kms"
            )
        if self.sse_algorithm == "aws:kms" and not self.kms_key_id:
            raise ValueError("使用 aws:kms 时必须配置 ARTIFACT_KMS_KEY_ID")
        self.client = client or boto3.client(
            "s3",
            endpoint_url=settings.artifact_endpoint,
            region_name=settings.artifact_region,
        )

    async def put_json(
        self,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        artifact_type: ArtifactType,
        logical_key: str,
        schema_version: str,
        payload: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactDescriptor:
        content = self._serialize_json(payload)
        content_sha256 = hashlib.sha256(content).hexdigest()
        object_key = self._build_object_key(
            tenant_id=tenant_id,
            job_id=job_id,
            logical_key=logical_key,
            content_sha256=content_sha256,
        )
        await asyncio.to_thread(
            self._put_object,
            object_key,
            content,
            content_sha256,
            metadata or {},
        )
        return ArtifactDescriptor(
            artifact_type=artifact_type,
            logical_key=logical_key,
            schema_version=schema_version,
            storage_uri=self._storage_uri(object_key),
            content_sha256=content_sha256,
            content_size=len(content),
            metadata=metadata or {},
        )

    async def get_json(
        self,
        *,
        storage_uri: str,
        expected_sha256: str,
    ) -> dict[str, Any]:
        object_key = self._parse_storage_uri(storage_uri)
        try:
            response = await asyncio.to_thread(
                self.client.get_object,
                Bucket=self.bucket,
                Key=object_key,
            )
            content = await asyncio.to_thread(response["Body"].read)
        except (BotoCoreError, ClientError, KeyError) as exc:
            raise ArtifactStoreError(f"读取 Artifact 失败: {storage_uri}") from exc
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sha256 != expected_sha256:
            raise ArtifactIntegrityError(
                f"Artifact SHA-256 校验失败: expected={expected_sha256}, "
                f"actual={actual_sha256}"
            )
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactIntegrityError("Artifact 内容不是合法 JSON") from exc
        if not isinstance(value, dict):
            raise ArtifactIntegrityError("Artifact JSON 顶层必须是对象")
        return value

    def _put_object(
        self,
        object_key: str,
        content: bytes,
        content_sha256: str,
        metadata: dict[str, Any],
    ) -> None:
        checksum = base64.b64encode(bytes.fromhex(content_sha256)).decode("ascii")
        request: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": object_key,
            "Body": content,
            "ContentType": "application/json",
            "ChecksumSHA256": checksum,
            "Metadata": {
                self._metadata_key(key): self._metadata_value(value)
                for key, value in metadata.items()
                if value is not None
            },
        }
        # 企业对象存储使用 AES256/KMS。仅允许在不具备 KMS 的容器化
        # 联调 MinIO 中显式使用 none，避免发送 MinIO 无法处理的 SSE 头。
        if self.sse_algorithm != "none":
            request["ServerSideEncryption"] = self.sse_algorithm
        if self.sse_algorithm == "aws:kms":
            request["SSEKMSKeyId"] = self.kms_key_id
        try:
            self.client.put_object(**request)
        except (BotoCoreError, ClientError) as exc:
            raise ArtifactStoreError(
                f"上传 Artifact 失败: s3://{self.bucket}/{object_key}"
            ) from exc

    def _build_object_key(
        self,
        *,
        tenant_id: uuid.UUID,
        job_id: uuid.UUID,
        logical_key: str,
        content_sha256: str,
    ) -> str:
        safe_logical_key = self._safe_logical_key(logical_key)
        return "/".join(
            part
            for part in [
                self.prefix,
                "tenants",
                str(tenant_id),
                "jobs",
                str(job_id),
                safe_logical_key,
                f"{content_sha256}.json",
            ]
            if part
        )

    def _storage_uri(self, object_key: str) -> str:
        return f"s3://{self.bucket}/{object_key}"

    def _parse_storage_uri(self, storage_uri: str) -> str:
        prefix = f"s3://{self.bucket}/"
        if not storage_uri.startswith(prefix):
            raise ArtifactStoreError("Artifact URI 不属于当前 Bucket")
        object_key = storage_uri[len(prefix) :]
        if not object_key or ".." in object_key.split("/"):
            raise ArtifactStoreError("Artifact URI 对象路径无效")
        required_prefix = f"{self.prefix}/" if self.prefix else ""
        if required_prefix and not object_key.startswith(required_prefix):
            raise ArtifactStoreError("Artifact URI 不属于当前服务前缀")
        return object_key

    @staticmethod
    def _serialize_json(payload: dict[str, Any]) -> bytes:
        try:
            text = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactStoreError("Artifact 无法序列化为规范 JSON") from exc
        return text.encode("utf-8")

    @staticmethod
    def _safe_logical_key(value: str) -> str:
        safe_value = re.sub(
            r"[^a-zA-Z0-9._:-]+", "-", value.strip()
        ).strip("-")
        if not safe_value or safe_value in {".", ".."}:
            raise ArtifactStoreError("Artifact logical_key 无效")
        return safe_value[:160]

    @staticmethod
    def _metadata_key(value: str) -> str:
        safe_value = re.sub(
            r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()
        ).strip("-")
        return safe_value[:64] or "metadata"

    @staticmethod
    def _metadata_value(value: Any) -> str:
        """限制 S3 用户元数据长度，避免把完整正文或秘密写入 Header。"""
        return str(value)[:1024]
