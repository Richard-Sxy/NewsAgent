import hashlib
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.domain.errors import ArtifactStoreError
from app.services.data_loop.dataset_freezer import (
    EvaluationDatasetIntegrityError,
    S3EvaluationDatasetArtifactStore,
)


def _settings():
    return SimpleNamespace(
        artifact_bucket="evaluation-bucket",
        artifact_prefix="news-agent",
        artifact_sse_algorithm="AES256",
        artifact_kms_key_id=None,
        artifact_endpoint=None,
        artifact_region="us-east-1",
    )


@pytest.mark.asyncio
async def test_s3_dataset_store_writes_content_addressed_json_and_reads_it():
    client = MagicMock()
    content = b'{"cases":[],"schema_version":"1.0"}'
    digest = hashlib.sha256(content).hexdigest()
    client.get_object.return_value = {"Body": BytesIO(content)}
    store = S3EvaluationDatasetArtifactStore(_settings(), client=client)

    receipt = await store.put_json(
        tenant_id="tenant/one",
        dataset_id=UUID("20000000-0000-0000-0000-000000000001"),
        dataset_name="daily cases",
        dataset_version="2026/09/09",
        content=content,
        content_sha256=digest,
    )
    loaded = await store.get_json(
        storage_uri=receipt.storage_uri,
        expected_sha256=digest,
    )

    assert loaded == {"cases": [], "schema_version": "1.0"}
    assert "%2F" in receipt.storage_uri
    request = client.put_object.call_args.kwargs
    assert request["Body"] == content
    assert request["ServerSideEncryption"] == "AES256"
    assert request["Metadata"]["content-sha256"] == digest


@pytest.mark.asyncio
async def test_s3_dataset_store_rejects_tampered_content_and_foreign_uri():
    client = MagicMock()
    client.get_object.return_value = {"Body": BytesIO(b'{"tampered":true}')}
    store = S3EvaluationDatasetArtifactStore(_settings(), client=client)

    with pytest.raises(EvaluationDatasetIntegrityError, match="checksum"):
        await store.get_json(
            storage_uri="s3://evaluation-bucket/news-agent/object.json",
            expected_sha256="a" * 64,
        )

    with pytest.raises(ArtifactStoreError, match="bucket"):
        await store.get_json(
            storage_uri="s3://another-bucket/object.json",
            expected_sha256="a" * 64,
        )
