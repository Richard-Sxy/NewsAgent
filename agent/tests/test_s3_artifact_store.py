import hashlib
import io
import uuid
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from botocore.exceptions import EndpointConnectionError

from app.domain.errors import ArtifactIntegrityError, ArtifactStoreError
from app.domain.execution import ArtifactType
from app.storage.s3 import S3ArtifactStore


@pytest.fixture(autouse=True)
def run_blocking_calls_inline(monkeypatch):
    """测试沙箱不允许创建线程；生产环境仍使用 asyncio.to_thread。"""

    async def inline(function: Callable, *args: Any, **kwargs: Any):
        return function(*args, **kwargs)

    monkeypatch.setattr("app.storage.s3.asyncio.to_thread", inline)


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.last_put: dict | None = None
        self.fail_put = False

    def put_object(self, **request) -> None:
        if self.fail_put:
            raise EndpointConnectionError(endpoint_url="https://s3.example.com")
        self.last_put = request
        self.objects[(request["Bucket"], request["Key"])] = request["Body"]

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


def settings(**overrides):
    values = {
        "artifact_bucket": "news-artifacts",
        "artifact_prefix": "news-writing",
        "artifact_sse_algorithm": "AES256",
        "artifact_kms_key_id": None,
        "artifact_endpoint": "https://s3.example.com",
        "artifact_region": "ap-shanghai",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_put_and_get_json_are_content_addressed_and_deterministic() -> None:
    client = FakeS3Client()
    store = S3ArtifactStore(settings(), client=client)
    tenant_id, job_id = uuid.uuid4(), uuid.uuid4()

    first = await store.put_json(
        tenant_id=tenant_id,
        job_id=job_id,
        artifact_type=ArtifactType.RESEARCH_PACKAGE,
        logical_key="research/package v1",
        schema_version="1.0",
        payload={"中文": "新闻", "count": 2},
        metadata={"Request ID": "req-1"},
    )
    second = await store.put_json(
        tenant_id=tenant_id,
        job_id=job_id,
        artifact_type=ArtifactType.RESEARCH_PACKAGE,
        logical_key="research/package v1",
        schema_version="1.0",
        payload={"count": 2, "中文": "新闻"},
    )

    assert first.content_sha256 == second.content_sha256
    assert first.storage_uri == second.storage_uri
    assert f"/tenants/{tenant_id}/jobs/{job_id}/" in first.storage_uri
    assert client.last_put["ServerSideEncryption"] == "AES256"
    assert client.last_put["ChecksumSHA256"]
    assert await store.get_json(
        storage_uri=first.storage_uri,
        expected_sha256=first.content_sha256,
    ) == {"count": 2, "中文": "新闻"}


@pytest.mark.asyncio
async def test_get_json_rejects_corrupted_content() -> None:
    client = FakeS3Client()
    store = S3ArtifactStore(settings(), client=client)
    key = "news-writing/corrupt.json"
    client.objects[("news-artifacts", key)] = b'{"changed":true}'

    with pytest.raises(ArtifactIntegrityError):
        await store.get_json(
            storage_uri=f"s3://news-artifacts/{key}",
            expected_sha256=hashlib.sha256(b'{"original":true}').hexdigest(),
        )


@pytest.mark.asyncio
async def test_upload_failure_is_classified_as_retryable() -> None:
    client = FakeS3Client()
    client.fail_put = True
    store = S3ArtifactStore(settings(), client=client)

    with pytest.raises(ArtifactStoreError) as error:
        await store.put_json(
            tenant_id=uuid.uuid4(),
            job_id=uuid.uuid4(),
            artifact_type=ArtifactType.DRAFT,
            logical_key="draft",
            schema_version="1",
            payload={"body": "text"},
        )
    assert error.value.retryable is True


def test_kms_requires_key_and_is_forwarded() -> None:
    with pytest.raises(ValueError, match="KMS_KEY_ID"):
        S3ArtifactStore(settings(artifact_sse_algorithm="aws:kms"), client=FakeS3Client())

    store = S3ArtifactStore(
        settings(artifact_sse_algorithm="aws:kms", artifact_kms_key_id="key-1"),
        client=FakeS3Client(),
    )
    store._put_object("news-writing/file.json", b"{}", hashlib.sha256(b"{}").hexdigest(), {})
    assert store.client.last_put["SSEKMSKeyId"] == "key-1"


@pytest.mark.asyncio
async def test_none_sse_omits_encryption_header() -> None:
    client = FakeS3Client()
    store = S3ArtifactStore(
        settings(artifact_sse_algorithm="none"),
        client=client,
    )

    await store.put_json(
        tenant_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        artifact_type=ArtifactType.RESEARCH_PACKAGE,
        logical_key="research_v1",
        schema_version="1.0",
        payload={"ok": True},
    )

    assert client.last_put is not None
    assert "ServerSideEncryption" not in client.last_put


def test_external_or_traversal_uri_is_rejected() -> None:
    store = S3ArtifactStore(settings(), client=FakeS3Client())
    with pytest.raises(ArtifactStoreError):
        store._parse_storage_uri("s3://another-bucket/news-writing/file.json")
    with pytest.raises(ArtifactStoreError):
        store._parse_storage_uri("s3://news-artifacts/news-writing/../secret")
