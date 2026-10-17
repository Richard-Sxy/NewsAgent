from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.modules.conversation.receipts import (
    CapabilityEntityType,
    CapabilityReceipt,
    CapabilityReceiptStatus,
)


def persisted_receipt(**overrides):
    values = {
        "entity_type": "writing_job",
        "entity_id": "job-001",
        "status": "landed",
        "version": "3",
        "deep_link": "/jobs?job_id=job-001",
        "created_at": datetime(2026, 10, 12, tzinfo=timezone.utc),
    }
    return CapabilityReceipt(**(values | overrides))


@pytest.mark.parametrize(
    ("entity_type", "path", "selector"),
    [
        ("hot_news_run", "/hot-news", "run_id"),
        ("writing_job", "/jobs", "job_id"),
        ("feedback_case", "/data-loop", "case_id"),
        ("memory", "/data-loop", "memory_id"),
        ("candidate", "/data-loop", "candidate_id"),
        ("evaluation_run", "/data-loop", "run_id"),
    ],
)
def test_receipt_accepts_each_entity_and_matching_local_target(entity_type, path, selector):
    receipt = persisted_receipt(entity_type=entity_type, deep_link=f"{path}?{selector}=job-001")
    assert receipt.entity_type == CapabilityEntityType(entity_type)
    assert receipt.status == CapabilityReceiptStatus.LANDED
    assert receipt.model_dump(mode="json")["version"] == "3"
    assert CapabilityReceipt.model_validate_json(receipt.model_dump_json()) == receipt
    assert persisted_receipt(entity_type=entity_type, deep_link=path).deep_link == path


def test_pending_receipt_retains_gate_and_authoritative_snapshot():
    receipt = persisted_receipt(status="pending_approval", pending_gate="research")
    assert receipt.pending_gate == "research"
    assert receipt.entity_id == "job-001"


def test_not_landed_receipt_does_not_claim_an_entity():
    receipt = persisted_receipt(
        status="not_landed", entity_id=None, version=None, deep_link=None
    )
    assert receipt.entity_id is None


@pytest.mark.parametrize("field,value", [("entity_type", "cms_publication"), ("status", "approved")])
def test_receipt_rejects_unknown_enum(field, value):
    with pytest.raises(ValidationError):
        persisted_receipt(**{field: value})


@pytest.mark.parametrize(
    "deep_link",
    [
        "https://example.com/jobs?job_id=job-001",
        "//example.com/jobs?job_id=job-001",
        "javascript:alert(1)",
        "/admin",
        "/jobs/",
        "/jobs/../admin",
        "/%6aobs?job_id=job-001",
        "/jobs#javascript:alert(1)",
        "/jobs#",
        "/jobs?",
        "/jobs?job_id=other-job",
        "/jobs?job_id=job-001&job_id=job-001",
        "/jobs?job_id=job-001&redirect=https://example.com",
        "/jobs?run_id=job-001",
        "/jobs?job_id=job-001&tenant_id=other",
        "/jobs?job_id=job-001%26redirect%3Dhttps://example.com",
        "/jobs?%6aob_id=job-001",
        "/jobs?job_id=%6aob-001",
        "/jobs?job_id",
        "/jobs?job_id=job-001\\evil",
        "\n/jobs?job_id=job-001",
        "/jobs?job_id=job-001\t",
        "/jobs?job_id=job-001\x00",
        "/data-loop?job_id=job-001",
    ],
)
def test_receipt_rejects_injected_or_mismatched_deep_link(deep_link):
    with pytest.raises(ValidationError):
        persisted_receipt(deep_link=deep_link)


@pytest.mark.parametrize(
    "field", ["entity_type", "entity_id", "status", "version", "deep_link", "created_at"]
)
def test_receipt_requires_all_snapshot_fields(field):
    values = persisted_receipt().model_dump()
    del values[field]
    with pytest.raises(ValidationError):
        CapabilityReceipt.model_validate(values)


@pytest.mark.parametrize("status", ["landed", "pending_approval"])
@pytest.mark.parametrize("field", ["entity_id", "version"])
def test_persisted_receipt_requires_id_and_version(status, field):
    with pytest.raises(ValidationError):
        persisted_receipt(status=status, **{field: None})


@pytest.mark.parametrize(
    "field,value",
    [
        ("entity_id", ""),
        ("entity_id", "  job-001"),
        ("entity_id", "job/001"),
        ("entity_id", 123),
        ("entity_id", "x" * 257),
        ("version", ""),
        ("version", "v1\n"),
        ("version", 3),
        ("version", "v" * 129),
        ("created_at", datetime(2026, 10, 12)),
        ("pending_gate", "review\n"),
        ("deep_link", 3),
    ],
)
def test_receipt_rejects_invalid_snapshot_values(field, value):
    with pytest.raises(ValidationError):
        persisted_receipt(**{field: value})


@pytest.mark.parametrize(
    "field,value",
    [("entity_id", "job-001"), ("version", "1"), ("deep_link", "/jobs"), ("pending_gate", "research")],
)
def test_not_landed_rejects_persistence_claims(field, value):
    with pytest.raises(ValidationError):
        persisted_receipt(
            **({"status": "not_landed", "entity_id": None, "version": None, "deep_link": None} | {field: value})
        )


def test_landed_receipt_cannot_claim_pending_gate_or_extra_privileges():
    with pytest.raises(ValidationError):
        persisted_receipt(pending_gate="research")
    with pytest.raises(ValidationError):
        persisted_receipt(approve=True)


def test_receipt_is_an_immutable_snapshot():
    receipt = persisted_receipt()
    with pytest.raises(ValidationError):
        receipt.version = "4"
