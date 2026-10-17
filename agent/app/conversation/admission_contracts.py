"""Bounded, versioned control records for conversation-only async admission."""

from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
from typing import Any, Literal
from uuid import UUID


ADMISSION_POLICY_VERSION = "conversation-async-admission-v1"
ADMISSION_FINGERPRINT_MEMO = "conversation_admission_fingerprint"
ADMISSION_TENANT_MEMO = "conversation_admission_tenant"
ADMISSION_BOOTSTRAP_LIMIT = 1000
ADMISSION_POLL_SECONDS = 3
AdmissionKind = Literal["writing", "evaluation"]


class AdmissionConflictError(ValueError):
    pass


def admission_fingerprint(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > 65536:
        raise ValueError("admission_payload_too_large")
    return sha256(encoded).hexdigest()


def tenant_admission_id(tenant_id: str) -> str:
    return "conversation-admission-tenant-" + admission_fingerprint(
        {"tenant_id": tenant_id, "policy": ADMISSION_POLICY_VERSION})


def request_admission_id(tenant_id: str, business_workflow_id: str) -> str:
    return "conversation-admission-request-" + admission_fingerprint(
        {"tenant_id": tenant_id, "workflow_id": business_workflow_id, "policy": ADMISSION_POLICY_VERSION})


def unavailable_admission_result(tenant_id: str, business_workflow_id: str) -> dict:
    """Unknown metadata; callers must independently prove ownership first."""
    return {"admission": {"state": "unknown", "queue_position": None, "active_count": None,
        "budget": None, "bootstrap_complete": None,
        "request_workflow_id": request_admission_id(tenant_id, business_workflow_id),
        "error_code": "admission_snapshot_unavailable"},
        "workflow_accepted": None, "admission_accepted": None}


@dataclass(frozen=True)
class AdmissionRequest:
    tenant_id: str
    kind: AdmissionKind
    business_workflow_id: str
    fingerprint: str
    payload: dict[str, Any]
    budget: int = 2
    queue_limit: int = 100
    policy_version: str = ADMISSION_POLICY_VERSION

    @property
    def request_workflow_id(self) -> str:
        return request_admission_id(self.tenant_id, self.business_workflow_id)

    @property
    def coordinator_workflow_id(self) -> str:
        return tenant_admission_id(self.tenant_id)

    def validate(self):
        if (not isinstance(self.tenant_id, str) or not self.tenant_id.strip()
                or len(self.tenant_id) > 128 or self.tenant_id != self.tenant_id.strip()):
            raise ValueError("admission_tenant_invalid")
        if self.kind not in {"writing", "evaluation"}:
            raise ValueError("admission_kind_invalid")
        if not isinstance(self.business_workflow_id, str) or not 1 <= len(self.business_workflow_id) <= 256:
            raise ValueError("admission_workflow_invalid")
        if not isinstance(self.fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", self.fingerprint) is None:
            raise ValueError("admission_fingerprint_invalid")
        if self.policy_version != ADMISSION_POLICY_VERSION:
            raise ValueError("admission_policy_invalid")
        if type(self.budget) is not int or not 1 <= self.budget <= 8:
            raise ValueError("admission_budget_invalid")
        if type(self.queue_limit) is not int or not 1 <= self.queue_limit <= 1000:
            raise ValueError("admission_queue_limit_invalid")
        if not isinstance(self.payload, dict):
            raise ValueError("admission_payload_invalid")
        admission_fingerprint(self.payload)
        if self.kind == "writing":
            if set(self.payload) != {"job_id"}:
                raise ValueError("admission_writing_payload_invalid")
            UUID(self.payload["job_id"])


@dataclass(frozen=True)
class AdmissionTicket:
    request_workflow_id: str
    business_workflow_id: str
    fingerprint: str


@dataclass
class TenantAdmissionState:
    tenant_id: str
    budget: int = 2
    queue_limit: int = 100
    initialized: bool = False
    pending: list[AdmissionTicket] = field(default_factory=list)
    active: list[AdmissionTicket] = field(default_factory=list)
    bootstrap_error: str | None = None
    observations: dict[str, str] = field(default_factory=dict)

    def snapshot(self, request_workflow_id: str = "") -> dict:
        pending = next((index for index, item in enumerate(self.pending)
                        if item.request_workflow_id == request_workflow_id), None)
        active = next((item for item in self.active if item.request_workflow_id == request_workflow_id), None)
        return {
            "state": "queued" if pending is not None else "granted" if active is not None else "not_found",
            "queue_position": pending + 1 if pending is not None else None,
            "active_count": len(self.active) if self.initialized else None,
            "budget": self.budget,
            "request_workflow_id": request_workflow_id,
            "bootstrap_complete": self.initialized,
            "error_code": self.bootstrap_error,
        }

    def enqueue(self, request: AdmissionRequest) -> dict:
        request.validate()
        if request.tenant_id != self.tenant_id:
            raise ValueError("admission_tenant_mismatch")
        if request.budget != self.budget or request.queue_limit != self.queue_limit:
            raise ValueError("admission_policy_conflict")
        ticket = AdmissionTicket(request.request_workflow_id, request.business_workflow_id, request.fingerprint)
        existing = next((item for item in [*self.pending, *self.active]
                         if item.request_workflow_id == ticket.request_workflow_id), None)
        if existing is not None:
            if existing != ticket:
                raise ValueError("admission_request_conflict")
            return self.snapshot(ticket.request_workflow_id)
        for index, active in enumerate(self.active):
            if active.business_workflow_id == ticket.business_workflow_id and not active.request_workflow_id:
                self.active[index] = ticket  # bind an imported task without adding a slot
                return self.snapshot(ticket.request_workflow_id)
        if len(self.pending) >= self.queue_limit:
            raise ValueError("admission_queue_full")
        self.pending.append(ticket)
        return self.snapshot(ticket.request_workflow_id)

    def grant(self) -> list[AdmissionTicket]:
        if not self.initialized:
            return []
        granted = []
        for pending in list(self.pending):
            for index, active in enumerate(self.active):
                if active.business_workflow_id == pending.business_workflow_id and not active.request_workflow_id:
                    self.active[index] = pending
                    self.pending.remove(pending)
                    granted.append(pending)
                    break
        while self.pending and len(self.active) < self.budget:
            item = self.pending.pop(0)
            self.active.append(item)
            granted.append(item)
        return granted

    def complete(self, business_workflow_id: str) -> list[AdmissionTicket]:
        finished = [item for item in self.active if item.business_workflow_id == business_workflow_id]
        self.active = [item for item in self.active if item.business_workflow_id != business_workflow_id]
        self.observations.pop(business_workflow_id, None)
        return finished
