import hashlib
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from app.domain.errors import ArtifactIntegrityError, ArtifactStoreError
from app.services.data_loop.artifacts import S3DataLoopArtifactStore


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
async def test_data_loop_store_uses_conditional_content_addressed_write():
    client = MagicMock()
    store = S3DataLoopArtifactStore(_settings(), client=client)

    receipt = await store.put_json(
        tenant_id="tenant/one",
        artifact_kind="error-attribution",
        logical_id="case/one",
        payload={"category": "evidence_gap", "score": 0.4},
    )

    request = client.put_object.call_args.kwargs
    assert request["IfNoneMatch"] == "*"
    assert request["Body"] == b'{"category":"evidence_gap","score":0.4}'
    assert request["Metadata"] == {
        "content-sha256": receipt.content_sha256,
        "artifact-kind": "data-loop",
    }
    assert "%2F" in receipt.storage_uri


@pytest.mark.asyncio
async def test_data_loop_store_treats_matching_preexisting_object_as_idempotent():
    client = MagicMock()
    payload = {"category": "evidence_gap", "score": 0.4}
    content = b'{"category":"evidence_gap","score":0.4}'
    digest = hashlib.sha256(content).hexdigest()
    client.put_object.side_effect = _conditional_conflict(
        code="PreconditionFailed",
        status=412,
    )
    client.get_object.return_value = {
        "Body": BytesIO(content),
        "Metadata": {
            "Content-SHA256": digest,
            "artifact-kind": "data-loop",
        },
    }
    store = S3DataLoopArtifactStore(_settings(), client=client)

    receipt = await store.put_json(
        tenant_id="tenant-one",
        artifact_kind="error-attribution",
        logical_id="case-one",
        payload=payload,
    )

    assert receipt.content_sha256 == digest
    client.get_object.assert_called_once()


@pytest.mark.asyncio
async def test_data_loop_store_rejects_conflicting_preexisting_content():
    client = MagicMock()
    content = b'{"category":"evidence_gap","score":0.4}'
    digest = hashlib.sha256(content).hexdigest()
    client.put_object.side_effect = _conditional_conflict(
        code="ConditionalRequestConflict",
        status=409,
    )
    client.get_object.return_value = {
        "Body": BytesIO(b'{"category":"tampered","score":0.4}'),
        "Metadata": {
            "content-sha256": digest,
            "artifact-kind": "data-loop",
        },
    }
    store = S3DataLoopArtifactStore(_settings(), client=client)

    with pytest.raises(ArtifactIntegrityError, match="content-addressed"):
        await store.put_json(
            tenant_id="tenant-one",
            artifact_kind="error-attribution",
            logical_id="case-one",
            payload={"category": "evidence_gap", "score": 0.4},
        )


@pytest.mark.asyncio
async def test_data_loop_store_rejects_conflicting_integrity_metadata():
    client = MagicMock()
    content = b'{"category":"evidence_gap","score":0.4}'
    client.put_object.side_effect = _conditional_conflict(
        code="412",
        status=412,
    )
    client.get_object.return_value = {
        "Body": BytesIO(content),
        "Metadata": {
            "content-sha256": "0" * 64,
            "artifact-kind": "data-loop",
        },
    }
    store = S3DataLoopArtifactStore(_settings(), client=client)

    with pytest.raises(ArtifactIntegrityError, match="metadata"):
        await store.put_json(
            tenant_id="tenant-one",
            artifact_kind="error-attribution",
            logical_id="case-one",
            payload={"category": "evidence_gap", "score": 0.4},
        )


@pytest.mark.asyncio
async def test_data_loop_store_reports_failed_conflict_read_as_store_error():
    client = MagicMock()
    client.put_object.side_effect = _conditional_conflict(
        code="PreconditionFailed",
        status=412,
    )
    client.get_object.side_effect = ClientError(
        {
            "Error": {"Code": "NoSuchKey", "Message": "missing"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        },
        "GetObject",
    )
    store = S3DataLoopArtifactStore(_settings(), client=client)

    with pytest.raises(ArtifactStoreError, match="failed to verify"):
        await store.put_json(
            tenant_id="tenant-one",
            artifact_kind="candidate-evaluation",
            logical_id="candidate-one",
            payload={"score": 0.4},
        )


def _conditional_conflict(*, code: str, status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "object already exists"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "PutObject",
    )
