import hashlib
import json
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


class _MemoryS3:
    def __init__(self):
        self.objects = {}
        self.fail_receipt_once = False
        self.receipt_race_winner = None
        self.write_count = 0

    def put_object(self, **request):
        key = request["Key"]
        self.write_count += 1
        if key.endswith("/receipt.json"):
            if self.fail_receipt_once:
                self.fail_receipt_once = False
                raise ClientError({"Error": {"Code": "InternalError"}}, "PutObject")
            if self.receipt_race_winner is not None:
                self.objects[key] = self.receipt_race_winner
                self.receipt_race_winner = None
        if key in self.objects:
            raise _conditional_conflict(code="PreconditionFailed", status=412)
        self.objects[key] = (request["Body"], dict(request["Metadata"]))

    def get_object(self, **request):
        if request["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        content, metadata = self.objects[request["Key"]]
        return {"Body": BytesIO(content), "Metadata": dict(metadata)}


def _analysis_arguments(payload):
    return {
        "tenant_id": "tenant/one",
        "artifact_kind": "dataset-analysis",
        "logical_id": "analysis/one",
        "payload": payload,
    }


def _receipt_key(client):
    return next(key for key in client.objects if key.endswith("/receipt.json"))


@pytest.mark.asyncio
async def test_data_loop_store_persists_and_reads_first_logical_report():
    client = _MemoryS3()
    store = S3DataLoopArtifactStore(_settings(), client=client)
    arguments = _analysis_arguments({"version": "first", "count": 2})
    identity = {key: value for key, value in arguments.items() if key != "payload"}
    assert await store.find_json(**identity) is None

    first = await store.put_json_once(**arguments)
    assert await store.find_json(**identity) == first
    writes = client.write_count
    assert await store.put_json_once(**_analysis_arguments({"version": "changed"})) == first
    assert client.write_count == writes
    assert first[1] == arguments["payload"]
    assert "%2F" in first[0].storage_uri
    pointer, metadata = client.objects[_receipt_key(client)]
    assert metadata == {
        "content-sha256": hashlib.sha256(pointer).hexdigest(),
        "artifact-kind": "data-loop-receipt",
    }


@pytest.mark.asyncio
async def test_data_loop_store_returns_receipt_race_winner_and_its_report():
    client = _MemoryS3()
    store = S3DataLoopArtifactStore(_settings(), client=client)
    winner = await store.put_json_once(**_analysis_arguments({"version": "winner"}))
    key = _receipt_key(client)
    client.receipt_race_winner = client.objects.pop(key)

    result = await store.put_json_once(**_analysis_arguments({"version": "loser"}))
    assert result == winner
    assert result[1] == {"version": "winner"}


@pytest.mark.asyncio
async def test_data_loop_store_retries_after_report_write_and_receipt_failure():
    client = _MemoryS3()
    client.fail_receipt_once = True
    store = S3DataLoopArtifactStore(_settings(), client=client)
    arguments = _analysis_arguments({"count": 2})

    with pytest.raises(ArtifactStoreError, match="write.*receipt"):
        await store.put_json_once(**arguments)
    assert len(client.objects) == 1
    result = await store.put_json_once(**arguments)
    assert result[1] == {"count": 2}
    assert len(client.objects) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", [
    "metadata_hash", "metadata_kind", "missing_metadata", "invalid_json", "shape",
    "tenant", "kind", "logical_id", "cross_tenant_path", "cross_bucket",
    "relative_path", "hash_filename", "digest", "size", "boolean_size", "report",
])
async def test_data_loop_store_rejects_corrupt_receipt_or_report(corruption):
    client = _MemoryS3()
    store = S3DataLoopArtifactStore(_settings(), client=client)
    receipt, _ = await store.put_json_once(**_analysis_arguments({"count": 2}))
    key = _receipt_key(client)
    content, metadata = client.objects[key]
    pointer = json.loads(content)
    if corruption == "metadata_hash":
        metadata["content-sha256"] = "0" * 64
    elif corruption == "metadata_kind":
        metadata["artifact-kind"] = "data-loop"
    elif corruption == "missing_metadata":
        metadata = {}
    elif corruption == "invalid_json":
        content = b"not-json"
        metadata["content-sha256"] = hashlib.sha256(content).hexdigest()
    elif corruption == "report":
        report_key = receipt.storage_uri.split("evaluation-bucket/", 1)[1]
        client.objects[report_key] = (b'{"count":3}', {})
    else:
        if corruption == "shape":
            pointer["unexpected"] = True
        elif corruption == "tenant":
            pointer["tenant_id"] = "other"
        elif corruption == "kind":
            pointer["artifact_kind"] = "candidate-evaluation"
        elif corruption == "logical_id":
            pointer["logical_id"] = "other"
        elif corruption == "cross_tenant_path":
            pointer["storage_uri"] = pointer["storage_uri"].replace("tenant%2Fone", "other")
        elif corruption == "cross_bucket":
            pointer["storage_uri"] = pointer["storage_uri"].replace("evaluation-bucket", "other")
        elif corruption == "relative_path":
            pointer["storage_uri"] = pointer["storage_uri"].replace("/data-loop/", "/../data-loop/")
        elif corruption == "hash_filename":
            pointer["storage_uri"] = pointer["storage_uri"].replace(receipt.content_sha256, "0" * 64)
        elif corruption == "digest":
            pointer["content_sha256"] = "G" * 64
        elif corruption == "size":
            pointer["content_size"] += 1
        elif corruption == "boolean_size":
            pointer["content_size"] = True
        content = store._canonical_bytes(pointer)
        metadata["content-sha256"] = hashlib.sha256(content).hexdigest()
    client.objects[key] = content, metadata

    with pytest.raises(ArtifactIntegrityError):
        await store.find_json(
            tenant_id="tenant/one", artifact_kind="dataset-analysis", logical_id="analysis/one"
        )


@pytest.mark.asyncio
async def test_data_loop_store_reports_receipt_read_errors_without_treating_them_as_missing():
    client = MagicMock()
    client.get_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied"}}, "GetObject"
    )
    store = S3DataLoopArtifactStore(_settings(), client=client)
    with pytest.raises(ArtifactStoreError, match="read.*receipt"):
        await store.find_json(
            tenant_id="tenant-one", artifact_kind="dataset-analysis", logical_id="analysis-one"
        )


def _conditional_conflict(*, code: str, status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "object already exists"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "PutObject",
    )
