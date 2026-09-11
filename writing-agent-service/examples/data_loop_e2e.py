"""Executable Data Loop acceptance driver with a real human decision stop.

The driver talks to the public management API and a real Temporal server.  It
does not seed Data Loop tables directly: the first feedback case is produced by
an actual persisted HotNews Workflow run and an operator rejection.

Typical manual flow::

    python -m examples.data_loop_e2e prepare --state /tmp/data-loop.json
    python -m examples.data_loop_e2e decide --state /tmp/data-loop.json
    python -m examples.data_loop_e2e verify --state /tmp/data-loop.json
    python -m examples.data_loop_e2e rollback --state /tmp/data-loop.json

To exercise both human label duties as explicit resumable stops, replace the
first command with::

    python -m examples.data_loop_e2e prepare-feedback --state /tmp/data-loop.json
    python -m examples.data_loop_e2e label-submit --state /tmp/data-loop.json
    python -m examples.data_loop_e2e label-review --state /tmp/data-loop.json
    python -m examples.data_loop_e2e prepare --state /tmp/data-loop.json

``decide`` deliberately prompts for the literal word APPROVE or REJECT unless
``--action`` is supplied by an automated test.  The state file contains only
public test identifiers and immutable Bundle specs; the gateway token is never
written to disk or printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from pydantic import TypeAdapter, ValidationError
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy

from app.schemas.production_bundle import ProductionBundleSpec
from app.services.hot_news_orchestration import HotNewsRunRequest
from app.services.production_bundle import ASSET_SPEC_FIELDS
from app.services.production_bundle_runtime import (
    ProductionBundleRuntimeRegistry,
    UnsupportedProductionBundleRuntimeError,
)
from app.workflows.contracts import HotNewsActivityOutcome
from app.workflows.hot_news import HOT_NEWS_WORKFLOW_NAME, HotNewsMonitorWorkflow
from examples.hot_news_e2e_support import build_hot_news_e2e_dependencies


DEFAULT_STATE_PATH = Path("/tmp/newsagent-data-loop-e2e.json")
DEFAULT_MANIFEST_PATH = (
    Path(__file__).parent / "data" / "data_loop_runtime_manifest.e2e.json"
)
DATA_LOOP_PREFIX = "/api/v1/data-loop"

ActorName = Literal[
    "candidate_manager",
    "feedback_writer",
    "labeler",
    "label_reviewer",
    "dataset_manager",
    "runner",
    "release_reviewer",
    "rollback_operator",
    "reader",
]

ROLE_BY_ACTOR: dict[ActorName, str] = {
    "candidate_manager": "data-loop:candidate-manage",
    "feedback_writer": "data-loop:feedback-write",
    "labeler": "data-loop:label-submit",
    "label_reviewer": "data-loop:label-approve",
    "dataset_manager": "data-loop:dataset-manage",
    "runner": "data-loop:run",
    "release_reviewer": "data-loop:release-approve",
    "rollback_operator": "data-loop:rollback",
    "reader": "data-loop:read",
}


class E2EFailure(RuntimeError):
    """An acceptance assertion or external dependency failed."""


@dataclass(slots=True)
class E2EState:
    tag: str
    tenant_id: str
    base_bundle_version: str
    candidate_bundle_version: str
    base_spec: dict[str, Any]
    candidate_spec: dict[str, Any]
    actors: dict[str, str]
    schema_version: str = "1.0"
    base_bundle_id: str | None = None
    analysis_run_id: str | None = None
    analysis_run_key: str | None = None
    news_id: str | None = None
    operator_decision_id: str | None = None
    feedback_case_id: str | None = None
    feedback_window_start: str | None = None
    feedback_window_end: str | None = None
    label_id: str | None = None
    label_version: int | None = None
    golden_dataset_id: str | None = None
    golden_dataset_sha256: str | None = None
    high_risk_dataset_id: str | None = None
    high_risk_dataset_sha256: str | None = None
    candidate_id: str | None = None
    workflow_id: str | None = None
    fresh_dataset_id: str | None = None
    evaluation_run_id: str | None = None
    decision: str | None = None
    terminal_phase: str | None = None
    activated_bundle_id: str | None = None
    recovery_workflow_id: str | None = None
    post_activation_run_id: str | None = None
    post_activation_run_key: str | None = None
    rolled_back: bool = False
    manual_label_gate: bool = False
    label_review_decision: str | None = None
    label_review_reason: str | None = None
    checks: dict[str, bool] = field(default_factory=dict)
    updated_at: str | None = None

    @classmethod
    def create(
        cls,
        *,
        tag: str,
        tenant_id: str,
        base_spec: ProductionBundleSpec,
        candidate_spec: ProductionBundleSpec,
    ) -> "E2EState":
        tenant_uuid = UUID(tenant_id)
        actors = {
            actor: str(
                uuid5(
                    tenant_uuid,
                    f"newsagent:data-loop-e2e:{actor}",
                )
            )
            for actor in ROLE_BY_ACTOR
        }
        return cls(
            tag=tag,
            tenant_id=str(tenant_uuid),
            base_bundle_version=f"e2e-{tag}-base",
            candidate_bundle_version=f"e2e-{tag}-candidate",
            base_spec=base_spec.model_dump(mode="json"),
            candidate_spec=candidate_spec.model_dump(mode="json"),
            actors=actors,
        )

    @classmethod
    def load(cls, path: Path) -> "E2EState":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("state root must be an object")
            state = cls(**payload)
            state.validate()
            return state
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise E2EFailure(f"cannot load E2E state {path}: {exc}") from exc

    def save(self, path: Path) -> None:
        self.validate()
        self.updated_at = datetime.now(UTC).isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)

    def validate(self) -> None:
        if self.schema_version != "1.0":
            raise E2EFailure(f"unsupported E2E state version: {self.schema_version}")
        UUID(self.tenant_id)
        expected_actors = set(ROLE_BY_ACTOR)
        if set(self.actors) != expected_actors:
            raise E2EFailure("state does not contain the complete actor set")
        actor_ids = [UUID(value) for value in self.actors.values()]
        if len(actor_ids) != len(set(actor_ids)):
            raise E2EFailure("every E2E duty must use a distinct user UUID")
        ProductionBundleSpec.model_validate(self.base_spec)
        ProductionBundleSpec.model_validate(self.candidate_spec)
        if self.label_review_decision not in {None, "approve", "reject"}:
            raise E2EFailure(
                "label_review_decision must be approve, reject, or null"
            )
        if self.label_review_decision is not None and not self.manual_label_gate:
            raise E2EFailure(
                "label_review_decision requires the opt-in manual label gate"
            )
        if self.label_review_decision is None and self.label_review_reason is not None:
            raise E2EFailure(
                "label_review_reason requires a recorded label review decision"
            )
        if self.label_review_decision is not None and not str(
            self.label_review_reason or ""
        ).strip():
            raise E2EFailure(
                "a recorded label review decision requires a non-empty reason"
            )

    def require(self, *names: str) -> None:
        missing = [name for name in names if getattr(self, name) in (None, "")]
        if missing:
            raise E2EFailure(f"state is missing required fields: {missing}")

    def check(self, name: str, condition: bool) -> None:
        self.checks[name] = bool(condition)
        if not condition:
            raise E2EFailure(f"acceptance assertion failed: {name}")


class DataLoopApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        gateway_token: str,
        state: E2EState,
        timeout_seconds: float = 60,
    ) -> None:
        if not gateway_token.strip():
            raise E2EFailure("DATA_LOOP_GATEWAY_TOKEN is required")
        self._base_url = base_url.rstrip("/")
        self._token = gateway_token
        self._state = state
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def close(self) -> None:
        await self._client.aclose()

    async def preflight(self) -> None:
        for path in ("/health", "/ready"):
            response = await self._client.get(f"{self._base_url}{path}")
            if response.status_code != 200:
                raise E2EFailure(
                    f"API preflight {path} failed with {response.status_code}: "
                    f"{response.text[:1000]}"
                )

    async def request(
        self,
        method: str,
        path: str,
        *,
        actor: ActorName,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        expected_status: int | tuple[int, ...] = 200,
        role_override: str | None = None,
    ) -> dict[str, Any]:
        expected = (
            (expected_status,)
            if isinstance(expected_status, int)
            else expected_status
        )
        response = await self._client.request(
            method,
            f"{self._base_url}{DATA_LOOP_PREFIX}{path}",
            headers={
                "Authorization": f"Bearer {self._token}",
                "X-Tenant-ID": self._state.tenant_id,
                "X-User-ID": self._state.actors[actor],
                "X-Data-Loop-Roles": role_override or ROLE_BY_ACTOR[actor],
            },
            json=json_body,
            params=params,
        )
        if response.status_code not in expected:
            raise E2EFailure(
                f"{method} {path} returned {response.status_code}, expected "
                f"{expected}: {response.text[:2000]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise E2EFailure(
                f"{method} {path} returned non-JSON content"
            ) from exc
        if not isinstance(payload, dict):
            raise E2EFailure(f"{method} {path} response root is not an object")
        return payload


class TemporalHotNewsRunner:
    def __init__(
        self,
        *,
        address: str,
        namespace: str,
        task_queue: str,
        timeout_seconds: float,
    ) -> None:
        self._address = address
        self._namespace = namespace
        self._task_queue = task_queue
        self._timeout_seconds = timeout_seconds
        self._client: Client | None = None

    async def run(
        self,
        *,
        tenant_id: str,
        production_bundle_version: str,
    ) -> tuple[HotNewsActivityOutcome, str]:
        dependencies = build_hot_news_e2e_dependencies(
            tenant_id=tenant_id,
            production_bundle_version=production_bundle_version,
        )
        request = HotNewsRunRequest(
            tenant_id=tenant_id,
            window_start=dependencies.window_start,
            window_end=dependencies.window_end,
            production_bundle_version=production_bundle_version,
            workflow_version=HOT_NEWS_WORKFLOW_NAME,
        )
        request.validate()
        try:
            async with asyncio.timeout(self._timeout_seconds):
                if self._client is None:
                    self._client = await Client.connect(
                        self._address,
                        namespace=self._namespace,
                    )
                handle = await self._client.start_workflow(
                    HotNewsMonitorWorkflow.run,
                    request,
                    id=request.idempotency_key,
                    task_queue=self._task_queue,
                    result_type=HotNewsActivityOutcome,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                )
                result: HotNewsActivityOutcome = await handle.result()
        except TimeoutError as exc:
            raise E2EFailure(
                "timed out waiting for the HotNews Workflow; verify the "
                "Temporal server and hot-news-worker"
            ) from exc
        if result.status != "completed" or result.analyzed_news_count < 1:
            raise E2EFailure(f"HotNews Workflow did not analyze any news: {result}")
        return result, dependencies.news_ids[0]


class FastGPTStubProbe:
    def __init__(self, base_url: str | None) -> None:
        self._base_url = None if not base_url else base_url.rstrip("/")

    @property
    def enabled(self) -> bool:
        return self._base_url is not None

    async def reset(self) -> None:
        if self._base_url is None:
            return
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self._base_url}/e2e/reset")
            if response.status_code != 200:
                raise E2EFailure("FastGPT E2E stub reset failed")

    async def mark(self) -> int:
        if self._base_url is None:
            return 0
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{self._base_url}/e2e/calls")
        if response.status_code != 200:
            raise E2EFailure("FastGPT E2E stub marker query failed")
        payload = response.json()
        value = (
            payload.get("latest_sequence")
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(value, int) or value < 0:
            raise E2EFailure("FastGPT E2E stub returned an invalid marker")
        return value

    async def calls(self, *, after_sequence: int = 0) -> list[dict[str, Any]]:
        if self._base_url is None:
            return []
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self._base_url}/e2e/calls",
                params={"after_sequence": after_sequence},
            )
        if response.status_code != 200:
            raise E2EFailure("FastGPT E2E stub call-log query failed")
        payload = response.json()
        calls = payload.get("calls") if isinstance(payload, dict) else None
        if not isinstance(calls, list):
            raise E2EFailure("FastGPT E2E stub returned an invalid call log")
        return [item for item in calls if isinstance(item, dict)]


class DataLoopE2EDriver:
    def __init__(
        self,
        *,
        state: E2EState,
        state_path: Path,
        api: DataLoopApiClient,
        hot_news: TemporalHotNewsRunner,
        stub_probe: FastGPTStubProbe,
        poll_seconds: float,
        timeout_seconds: float,
    ) -> None:
        self.state = state
        self.state_path = state_path
        self.api = api
        self.hot_news = hot_news
        self.stub_probe = stub_probe
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds

    async def close(self) -> None:
        await self.api.close()

    def checkpoint(self) -> None:
        self.state.save(self.state_path)

    async def prepare(self) -> dict[str, Any]:
        """Run the real chain until the Workflow is durably waiting for a human."""

        await self.api.preflight()
        await self._bootstrap_base_bundle()
        await self._run_initial_hot_news_window()
        await self._record_operator_feedback()
        await self._submit_and_approve_label()
        await self._freeze_baseline_datasets()
        await self._propose_candidate()
        await self._start_data_loop()
        snapshot = await self._wait_for_phase("waiting_approval")
        self.state.fresh_dataset_id = _required_string(snapshot, "dataset_id")
        self.state.evaluation_run_id = _required_string(
            snapshot, "evaluation_run_id"
        )
        self.state.check("gate_passed", snapshot.get("gate_passed") is True)
        self.state.check(
            "workflow_waiting_for_human",
            snapshot.get("waiting_for_approval") is True,
        )
        await self._assert_release_role_boundary()
        candidate = await self._get_candidate()
        self.state.check(
            "candidate_evaluation_passed",
            candidate.get("status") == "evaluation_passed",
        )
        self.checkpoint()
        report = self._report(snapshot=snapshot, candidate=candidate)
        _print_event("prepared_for_human_decision", report)
        return report

    async def prepare_feedback_for_human_label(self) -> dict[str, Any]:
        """Run only to a durable Feedback Case and require explicit label duties."""

        await self.api.preflight()
        if self.state.label_review_decision == "reject":
            raise E2EFailure(
                "the human label reviewer rejected this scenario; create a new "
                "label version or use a new --state path"
            )
        self.state.manual_label_gate = True
        self.checkpoint()
        await self._bootstrap_base_bundle()
        await self._run_initial_hot_news_window()
        await self._record_operator_feedback()
        if self.state.label_id is None:
            self.state.check("waiting_for_human_label_submission", True)
        self.checkpoint()
        report = self._report()
        _print_event("prepared_for_human_label_submission", report)
        return report

    async def submit_human_label(self) -> dict[str, Any]:
        """Submit the test label with the dedicated labeler identity only."""

        await self.api.preflight()
        if not self.state.manual_label_gate:
            raise E2EFailure(
                "manual label submission requires a state created by "
                "prepare-feedback"
            )
        if self.state.label_review_decision == "reject":
            raise E2EFailure(
                "the human reviewer already rejected this label scenario"
            )
        await self._submit_label()
        label = await self._get_state_label()
        self.state.check(
            "human_label_was_submitted_by_labeler",
            label.get("approval_status") in {"pending", "approved"}
            and label.get("labeled_by") == self.state.actors["labeler"],
        )
        if label.get("approval_status") == "pending":
            self.state.check(
                "human_label_submitted_pending_review",
                label.get("approved_by") is None,
            )
        self.checkpoint()
        report = self._report(label=label)
        _print_event("human_label_submitted", report)
        return report

    async def review_human_label(
        self,
        action: Literal["approve", "reject"],
        *,
        reason: str,
    ) -> dict[str, Any]:
        """Persist an independent approval or request-changes decision."""

        await self.api.preflight()
        if not self.state.manual_label_gate:
            raise E2EFailure(
                "manual label review requires a state created by prepare-feedback"
            )
        if action not in {"approve", "reject"}:
            raise E2EFailure("label review action must be approve or reject")
        self.state.require("feedback_case_id", "label_id", "label_version")
        if not reason.strip():
            raise E2EFailure("label review reason cannot be empty")
        if len(reason.strip()) > 500:
            raise E2EFailure("label review reason must not exceed 500 characters")
        if self.state.label_review_decision is not None:
            if self.state.label_review_decision != action:
                raise E2EFailure(
                    "state already contains label review decision "
                    f"{self.state.label_review_decision!r}"
                )
            if self.state.label_review_reason != reason.strip():
                raise E2EFailure(
                    "state already contains this label review action with a "
                    "different reason"
                )
            label = await self._get_state_label()
            report = self._report(label=label)
            _print_event("human_label_review_replayed", report)
            return report

        label = await self._get_state_label()
        if action == "approve" and label.get("approval_status") == "approved":
            self.state.check(
                "human_label_review_approved_independently",
                label.get("labeled_by") == self.state.actors["labeler"]
                and label.get("approved_by")
                == self.state.actors["label_reviewer"],
            )
            self.state.feedback_window_end = _required_string(
                label, "approved_at"
            )
            self.state.label_review_decision = "approve"
            self.state.label_review_reason = reason.strip()
            self.checkpoint()
            report = self._report(label=label)
            _print_event("human_label_review_recovered", report)
            return report
        if label.get("approval_status") != "pending":
            raise E2EFailure(
                "label is not pending an independent review: "
                f"status={label.get('approval_status')!r}"
            )
        if action == "reject":
            payload = await self.api.request(
                "POST",
                (
                    f"/feedback-cases/{self.state.feedback_case_id}/labels/"
                    f"{self.state.label_id}/review"
                ),
                actor="label_reviewer",
                json_body={
                    "action": "request_changes",
                    "expected_label_version": self.state.label_version,
                    "reason": reason.strip(),
                    "idempotency_key": self._key("label-review-reject"),
                },
            )
            label = _required_mapping(payload, "label")
            self.state.label_review_decision = "reject"
            self.state.label_review_reason = reason.strip()
            self.state.check(
                "human_label_review_rejected_safely",
                label.get("approval_status") == "rejected"
                and label.get("reviewed_by")
                == self.state.actors["label_reviewer"]
                and label.get("review_reason") == reason.strip()
                and label.get("approved_by") is None,
            )
            self.checkpoint()
            report = self._report(label=label)
            _print_event("human_label_review_rejected", report)
            return report

        await self._approve_label(review_reason=reason.strip())
        label = await self._get_state_label()
        self.state.check(
            "human_label_review_approved_independently",
            label.get("approval_status") == "approved"
            and label.get("labeled_by") == self.state.actors["labeler"]
            and label.get("approved_by")
            == self.state.actors["label_reviewer"],
        )
        self.state.label_review_decision = "approve"
        self.state.label_review_reason = reason.strip()
        self.checkpoint()
        report = self._report(label=label)
        _print_event("human_label_review_approved", report)
        return report

    async def decide(
        self,
        action: Literal["approve", "reject"],
        *,
        reason: str,
        verify_next_online_run: bool = True,
    ) -> dict[str, Any]:
        self.state.require("workflow_id", "candidate_id", "base_bundle_id")
        if not reason.strip():
            raise E2EFailure("decision reason cannot be empty")
        if len(reason.strip()) > 500:
            raise E2EFailure("decision reason must not exceed 500 characters")
        snapshot = await self._snapshot()
        if self.state.decision is None:
            if snapshot.get("phase") != "waiting_approval":
                raise E2EFailure(
                    "Workflow is not waiting for a decision: "
                    f"phase={snapshot.get('phase')!r}"
                )
            response = await self.api.request(
                "POST",
                f"/runs/{self.state.workflow_id}/decision",
                actor="release_reviewer",
                json_body={
                    "action": action,
                    "reason": reason.strip(),
                    "idempotency_key": self._key(f"release-{action}"),
                },
                expected_status=202,
            )
            self.state.check(
                "human_decision_submitted",
                response.get("status") == "submitted",
            )
            self.state.decision = action
            self.checkpoint()
        elif self.state.decision != action:
            raise E2EFailure(
                f"state already contains decision {self.state.decision!r}"
            )

        terminal = "activated" if action == "approve" else "rejected"
        snapshot = await self._wait_for_phase(terminal)
        self.state.terminal_phase = terminal
        candidate = await self._get_candidate()
        active = await self._get_active_bundle()

        if action == "approve":
            self.state.check(
                "candidate_activated",
                candidate.get("status") == "activated",
            )
            self.state.check(
                "active_bundle_is_candidate",
                active.get("bundle_version")
                == self.state.candidate_bundle_version,
            )
            self.state.activated_bundle_id = _required_string(active, "id")
            if verify_next_online_run:
                await self._verify_next_online_run()
        else:
            self.state.check(
                "candidate_rejected",
                candidate.get("status") == "rejected",
            )
            self.state.check(
                "rejection_keeps_base_active",
                active.get("id") == self.state.base_bundle_id,
            )

        self.checkpoint()
        report = self._report(
            snapshot=snapshot,
            candidate=candidate,
            active_bundle=active,
        )
        _print_event("human_decision_completed", report)
        return report

    async def verify(self) -> dict[str, Any]:
        self.state.require(
            "base_bundle_id",
            "candidate_id",
            "workflow_id",
            "golden_dataset_id",
            "high_risk_dataset_id",
            "fresh_dataset_id",
        )
        snapshot = await self._snapshot()
        candidate = await self._get_candidate()
        active = await self._get_active_bundle()
        datasets = []
        for dataset_id in (
            self.state.golden_dataset_id,
            self.state.high_risk_dataset_id,
            self.state.fresh_dataset_id,
        ):
            payload = await self.api.request(
                "GET",
                f"/datasets/{dataset_id}",
                actor="reader",
            )
            datasets.append(_required_mapping(payload, "dataset"))
        self.state.check(
            "three_distinct_dataset_ids",
            len({item.get("dataset_id") for item in datasets}) == 3,
        )
        self.state.check(
            "three_expected_dataset_layers",
            {item.get("dataset_layer") for item in datasets}
            == {"golden", "fresh_bad_case", "high_risk_regression"},
        )

        if self.state.rolled_back:
            self.state.check(
                "rollback_restored_base",
                active.get("id") == self.state.base_bundle_id,
            )
        elif self.state.decision == "approve":
            self.state.check(
                "active_bundle_is_candidate",
                active.get("bundle_version")
                == self.state.candidate_bundle_version,
            )
        elif self.state.decision == "reject":
            self.state.check(
                "rejection_keeps_base_active",
                active.get("id") == self.state.base_bundle_id,
            )
        else:
            self.state.check(
                "workflow_waiting_for_human",
                snapshot.get("phase") == "waiting_approval",
            )

        self.checkpoint()
        report = self._report(
            snapshot=snapshot,
            candidate=candidate,
            active_bundle=active,
            datasets=datasets,
        )
        _print_event("acceptance_state_verified", report)
        return report

    async def rollback(self, *, reason: str) -> dict[str, Any]:
        self.state.require("base_bundle_id")
        active = await self._get_active_bundle()
        if active.get("id") != self.state.base_bundle_id:
            response = await self.api.request(
                "POST",
                "/production-bundles/rollback",
                actor="rollback_operator",
                json_body={
                    "target_bundle_id": self.state.base_bundle_id,
                    "reason": reason.strip(),
                    "idempotency_key": self._key("rollback-to-base"),
                },
            )
            active = _required_mapping(response, "active_bundle")
            decision = _required_mapping(response, "decision")
            self.state.check(
                "rollback_ledger_created",
                decision.get("action") == "rollback",
            )
        self.state.check(
            "rollback_restored_base",
            active.get("id") == self.state.base_bundle_id,
        )
        self.state.rolled_back = True
        self.checkpoint()
        report = self._report(active_bundle=active)
        _print_event("rollback_completed", report)
        return report

    async def recover_activation(
        self,
        *,
        verify_next_online_run: bool = True,
    ) -> dict[str, Any]:
        """Recover only an activation already authorized by the human gate."""

        self.state.require("workflow_id", "candidate_id", "base_bundle_id")
        if self.state.rolled_back or self.state.terminal_phase == "activated":
            raise E2EFailure(
                "activation recovery cannot override a completed activation or "
                "a deliberate rollback; create a newly authorized roll-forward"
            )
        if self.state.decision != "approve":
            raise E2EFailure("activation recovery requires a recorded approval")
        if self.state.recovery_workflow_id is None:
            response = await self.api.request(
                "POST",
                f"/runs/{self.state.workflow_id}/activation/recover",
                actor="release_reviewer",
                json_body={
                    "idempotency_key": self._key("activation-recovery"),
                },
                expected_status=202,
            )
            self.state.recovery_workflow_id = _required_string(
                response, "recovery_workflow_id"
            )
            self.checkpoint()

        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        active: dict[str, Any] | None = None
        while asyncio.get_running_loop().time() < deadline:
            active = await self._get_active_bundle()
            if active.get("bundle_version") == self.state.candidate_bundle_version:
                break
            await asyncio.sleep(self.poll_seconds)
        else:
            raise E2EFailure(
                "activation recovery did not switch the active Bundle; "
                f"last active={active}"
            )

        candidate = await self._get_candidate()
        self.state.check(
            "activation_recovery_activated_candidate",
            candidate.get("status") == "activated",
        )
        self.state.check(
            "activation_recovery_switched_active_bundle",
            active is not None
            and active.get("bundle_version") == self.state.candidate_bundle_version,
        )
        assert active is not None
        self.state.activated_bundle_id = _required_string(active, "id")
        self.state.terminal_phase = "activation_recovered"
        if verify_next_online_run:
            await self._verify_next_online_run()
        self.checkpoint()
        report = self._report(candidate=candidate, active_bundle=active)
        _print_event("activation_recovery_completed", report)
        return report

    async def _bootstrap_base_bundle(self) -> None:
        if self.state.base_bundle_id is not None:
            return
        payload = await self.api.request(
            "POST",
            "/production-bundles/bootstrap",
            actor="candidate_manager",
            json_body={
                "bundle_version": self.state.base_bundle_version,
                "spec": self.state.base_spec,
            },
            expected_status=201,
        )
        bundle = _required_mapping(payload, "bundle")
        self.state.check("base_bundle_active", bundle.get("status") == "active")
        self.state.check(
            "base_bundle_version_matches",
            bundle.get("bundle_version") == self.state.base_bundle_version,
        )
        self.state.base_bundle_id = _required_string(bundle, "id")
        self.checkpoint()

    async def _run_initial_hot_news_window(self) -> None:
        if self.state.analysis_run_id is not None:
            return
        result, news_id = await self.hot_news.run(
            tenant_id=self.state.tenant_id,
            production_bundle_version=self.state.base_bundle_version,
        )
        self.state.analysis_run_id = result.run_id
        self.state.analysis_run_key = result.idempotency_key
        self.state.news_id = news_id
        self.state.check("initial_online_run_completed", result.status == "completed")
        self.state.check(
            "initial_online_run_analyzed_news",
            result.analyzed_news_count > 0,
        )
        self.checkpoint()

    async def _record_operator_feedback(self) -> None:
        self.state.require("analysis_run_id", "analysis_run_key", "news_id")
        if self.state.feedback_case_id is not None:
            return
        payload = await self.api.request(
            "POST",
            "/operator-decisions",
            actor="feedback_writer",
            json_body={
                "run_id": self.state.analysis_run_id,
                "news_id": self.state.news_id,
                "decision_type": "rejected",
                "reason": "E2E human operator marked the synthetic report for review",
                "correction_payload": {},
                "idempotency_key": self._key("operator-rejection"),
                "feedback_problem_type": "unsupported_claim",
                "feedback_severity": "high",
            },
            expected_status=201,
        )
        self.state.operator_decision_id = _required_string(
            payload, "decision_id"
        )
        cases_payload = await self.api.request(
            "GET",
            "/feedback-cases",
            actor="reader",
            params={"statuses": "needs_label", "limit": 200},
        )
        cases = cases_payload.get("cases")
        if not isinstance(cases, list):
            raise E2EFailure("feedback case list is missing cases")
        matching = [
            item
            for item in cases
            if isinstance(item, dict)
            and item.get("run_id") == self.state.analysis_run_id
            and item.get("news_id") == self.state.news_id
            and isinstance(item.get("source_reference"), dict)
            and item["source_reference"].get("reference_id")
            == self.state.operator_decision_id
        ]
        if len(matching) != 1:
            raise E2EFailure(
                f"expected one feedback case for operator decision, got {len(matching)}"
            )
        case = matching[0]
        self.state.feedback_case_id = _required_string(case, "id")
        occurred_at = _required_string(case, "occurred_at")
        occurred = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
        self.state.feedback_window_start = (
            occurred - timedelta(seconds=1)
        ).isoformat()
        self.state.check("operator_rejection_created_feedback", True)
        self.checkpoint()

    async def _submit_and_approve_label(self) -> None:
        if self.state.manual_label_gate:
            if self.state.label_review_decision == "reject":
                raise E2EFailure(
                    "the human label reviewer rejected this scenario; the Data "
                    "Loop cannot continue"
                )
            if self.state.label_id is None:
                raise E2EFailure(
                    "manual label submission is pending; run label-submit"
                )
            if self.state.label_review_decision != "approve":
                raise E2EFailure(
                    "independent human label review is pending; run label-review"
                )
            approved = await self._get_state_label()
            self.state.check(
                "human_label_review_approved_independently",
                approved.get("approval_status") == "approved"
                and approved.get("labeled_by") == self.state.actors["labeler"]
                and approved.get("approved_by")
                == self.state.actors["label_reviewer"],
            )
            self.state.feedback_window_end = _required_string(
                approved, "approved_at"
            )
            self.checkpoint()
            return
        else:
            await self._submit_label()
        await self._approve_label()

    async def _submit_label(self) -> None:
        self.state.require("feedback_case_id")
        if self.state.label_id is None:
            payload = await self.api.request(
                "POST",
                f"/feedback-cases/{self.state.feedback_case_id}/labels",
                actor="labeler",
                json_body={
                    "verdict": "incorrect",
                    "allowed_dominant_drivers": [
                        "click",
                        "consumption",
                        "interaction",
                        "growth",
                        "insufficient_data",
                    ],
                    "required_evidence_news_ids": [],
                    "forbidden_evidence_news_ids": [],
                    "required_metric_keys": [],
                    "must_state_limitation": False,
                    "operator_comment": (
                        "E2E pipeline label: permissive contract smoke gate"
                    ),
                    "expected_previous_version": None,
                    "idempotency_key": self._key("label-submit"),
                },
                expected_status=201,
            )
            label = _required_mapping(payload, "label")
            self.state.check(
                "label_starts_pending",
                label.get("approval_status") == "pending",
            )
            self.state.label_id = _required_string(label, "id")
            version = label.get("label_version")
            if not isinstance(version, int):
                raise E2EFailure("label response is missing label_version")
            self.state.label_version = version
            self.checkpoint()

    async def _approve_label(self, *, review_reason: str = "Label approved") -> None:
        self.state.require("label_id", "label_version")
        # First prove the role boundary. This call cannot mutate the label.
        denied = await self.api.request(
            "POST",
            (
                f"/feedback-cases/{self.state.feedback_case_id}/labels/"
                f"{self.state.label_id}/approve"
            ),
            actor="labeler",
            json_body={
                "expected_label_version": self.state.label_version,
                "idempotency_key": self._key("label-self-approval-denied"),
            },
            expected_status=403,
        )
        self.state.check(
            "labeler_cannot_use_approval_role",
            "permission" in str(denied.get("detail", "")),
        )
        self_review_denied = await self.api.request(
            "POST",
            (
                f"/feedback-cases/{self.state.feedback_case_id}/labels/"
                f"{self.state.label_id}/approve"
            ),
            actor="labeler",
            role_override=ROLE_BY_ACTOR["label_reviewer"],
            json_body={
                "expected_label_version": self.state.label_version,
                "idempotency_key": self._key("label-self-review-denied"),
            },
            expected_status=409,
        )
        self.state.check(
            "label_submitter_cannot_self_approve",
            "own label" in str(self_review_denied.get("detail", "")),
        )

        labels_payload = await self.api.request(
            "GET",
            f"/feedback-cases/{self.state.feedback_case_id}/labels",
            actor="reader",
        )
        labels = labels_payload.get("labels")
        approved = next(
            (
                item
                for item in labels or []
                if isinstance(item, dict)
                and item.get("id") == self.state.label_id
                and item.get("approval_status") == "approved"
            ),
            None,
        )
        if approved is None:
            payload = await self.api.request(
                "POST",
                (
                    f"/feedback-cases/{self.state.feedback_case_id}/labels/"
                    f"{self.state.label_id}/approve"
                ),
                actor="label_reviewer",
                json_body={
                    "expected_label_version": self.state.label_version,
                    "reason": review_reason,
                    "idempotency_key": self._key("label-approve"),
                },
            )
            approved = _required_mapping(payload, "label")
        self.state.check(
            "independent_label_review_completed",
            approved.get("approval_status") == "approved"
            and approved.get("labeled_by") == self.state.actors["labeler"]
            and approved.get("approved_by")
            == self.state.actors["label_reviewer"],
        )
        self.state.feedback_window_end = _required_string(
            approved, "approved_at"
        )
        self.checkpoint()

    async def _get_state_label(self) -> dict[str, Any]:
        self.state.require("feedback_case_id", "label_id")
        labels_payload = await self.api.request(
            "GET",
            f"/feedback-cases/{self.state.feedback_case_id}/labels",
            actor="reader",
        )
        labels = labels_payload.get("labels")
        label = next(
            (
                item
                for item in labels or []
                if isinstance(item, dict) and item.get("id") == self.state.label_id
            ),
            None,
        )
        if label is None:
            raise E2EFailure("state label was not returned by the public API")
        return label

    async def _freeze_baseline_datasets(self) -> None:
        self.state.require(
            "feedback_case_id", "feedback_window_end", "label_id"
        )
        if self.state.golden_dataset_id is None:
            dataset = await self._freeze_dataset(
                layer="golden",
                name=f"e2e-{self.state.tag}-golden",
                operation="freeze-golden",
            )
            self.state.golden_dataset_id = _required_string(
                dataset, "dataset_id"
            )
            self.state.golden_dataset_sha256 = _required_string(
                dataset, "content_sha256"
            )
            self.checkpoint()
        if self.state.high_risk_dataset_id is None:
            dataset = await self._freeze_dataset(
                layer="high_risk_regression",
                name=f"e2e-{self.state.tag}-high-risk",
                operation="freeze-high-risk",
            )
            self.state.high_risk_dataset_id = _required_string(
                dataset, "dataset_id"
            )
            self.state.high_risk_dataset_sha256 = _required_string(
                dataset, "content_sha256"
            )
            self.checkpoint()
        self.state.check(
            "baseline_dataset_ids_are_distinct",
            self.state.golden_dataset_id != self.state.high_risk_dataset_id,
        )

    async def _freeze_dataset(
        self,
        *,
        layer: str,
        name: str,
        operation: str,
    ) -> dict[str, Any]:
        payload = await self.api.request(
            "POST",
            "/datasets/freeze",
            actor="dataset_manager",
            json_body={
                "dataset_name": name,
                "dataset_version": self.state.tag,
                "dataset_layer": layer,
                "description": f"NewsAgent Data Loop E2E {layer} cohort",
                "feedback_case_ids": [self.state.feedback_case_id],
                "source_cutoff_at": self.state.feedback_window_end,
                "idempotency_key": self._key(operation),
            },
            expected_status=201,
        )
        dataset = _required_mapping(payload, "dataset")
        if dataset.get("dataset_layer") != layer or dataset.get("case_count") != 1:
            raise E2EFailure(f"frozen {layer} dataset has unexpected metadata")
        return dataset

    async def _propose_candidate(self) -> None:
        self.state.require("base_bundle_id")
        if self.state.candidate_id is not None:
            return
        base = ProductionBundleSpec.model_validate(self.state.base_spec)
        candidate = ProductionBundleSpec.model_validate(self.state.candidate_spec)
        payload = await self.api.request(
            "POST",
            "/configuration-candidates",
            actor="candidate_manager",
            json_body={
                "base_bundle_id": self.state.base_bundle_id,
                "candidate_version": self.state.candidate_bundle_version,
                "proposed_spec": self.state.candidate_spec,
                "structured_diff": build_structured_diff(base, candidate),
                "proposal_reason": (
                    "Exercise deterministic three-cohort Data Loop acceptance"
                ),
                "idempotency_key": self._key("candidate-proposal"),
            },
            expected_status=201,
        )
        proposed = _required_mapping(payload, "candidate")
        self.state.check(
            "candidate_pending_evaluation",
            proposed.get("status") == "pending_evaluation",
        )
        self.state.candidate_id = _required_string(proposed, "id")
        self.checkpoint()

    async def _start_data_loop(self) -> None:
        self.state.require(
            "feedback_window_start",
            "feedback_window_end",
            "golden_dataset_id",
            "high_risk_dataset_id",
            "candidate_id",
        )
        if self.state.workflow_id is not None:
            return
        payload = await self.api.request(
            "POST",
            "/runs",
            actor="runner",
            json_body={
                "window_start": self.state.feedback_window_start,
                "window_end": self.state.feedback_window_end,
                "dataset_name": f"e2e-{self.state.tag}-fresh",
                "dataset_version": self.state.tag,
                "golden_dataset_id": self.state.golden_dataset_id,
                "high_risk_regression_dataset_id": (
                    self.state.high_risk_dataset_id
                ),
                "candidate_id": self.state.candidate_id,
                "previous_experiment_candidate_id": None,
                "evaluation_policy_version": "hot-news-gate-v1",
                "idempotency_key": self._key("data-loop-run"),
            },
            expected_status=202,
        )
        self.state.workflow_id = _required_string(payload, "workflow_id")
        self.state.check("data_loop_run_accepted", payload.get("status") == "accepted")
        self.checkpoint()

    async def _assert_release_role_boundary(self) -> None:
        self.state.require("workflow_id")
        denied = await self.api.request(
            "POST",
            f"/runs/{self.state.workflow_id}/decision",
            actor="runner",
            json_body={
                "action": "approve",
                "reason": "this identity must not be allowed to approve",
                "idempotency_key": self._key("release-role-denied"),
            },
            expected_status=403,
        )
        self.state.check(
            "runner_cannot_approve_release",
            "permission" in str(denied.get("detail", "")),
        )

    async def _verify_next_online_run(self) -> None:
        if self.state.post_activation_run_id is not None:
            return
        call_marker = await self.stub_probe.mark()
        result, _ = await self.hot_news.run(
            tenant_id=self.state.tenant_id,
            production_bundle_version=self.state.candidate_bundle_version,
        )
        self.state.post_activation_run_id = result.run_id
        self.state.post_activation_run_key = result.idempotency_key
        self.state.check(
            "next_online_run_completed_on_candidate",
            result.status == "completed" and result.analyzed_news_count > 0,
        )
        if self.stub_probe.enabled:
            calls = await self.stub_probe.calls(after_sequence=call_marker)
            candidate_app_id = self.state.candidate_spec["fastgpt_app_id"]
            candidate_calls = [
                call
                for call in calls
                if call.get("app_id") == candidate_app_id
            ]
            self.state.check(
                "next_online_run_called_candidate_fastgpt_app",
                len(candidate_calls) >= result.analyzed_news_count,
            )
        self.checkpoint()

    async def _wait_for_phase(self, expected_phase: str) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        last: dict[str, Any] | None = None
        while asyncio.get_running_loop().time() < deadline:
            last = await self._snapshot()
            phase = last.get("phase")
            if phase == expected_phase:
                return last
            if phase == "evaluation_failed":
                raise E2EFailure(
                    "candidate failed deterministic gate; inspect evaluation "
                    f"run {last.get('evaluation_run_id')}"
                )
            await asyncio.sleep(self.poll_seconds)
        raise E2EFailure(
            f"timed out waiting for phase {expected_phase!r}; last snapshot={last}"
        )

    async def _snapshot(self) -> dict[str, Any]:
        self.state.require("workflow_id")
        return await self.api.request(
            "GET",
            f"/runs/{self.state.workflow_id}",
            actor="reader",
        )

    async def _get_candidate(self) -> dict[str, Any]:
        self.state.require("candidate_id")
        payload = await self.api.request(
            "GET",
            f"/configuration-candidates/{self.state.candidate_id}",
            actor="reader",
        )
        return _required_mapping(payload, "candidate")

    async def _get_active_bundle(self) -> dict[str, Any]:
        payload = await self.api.request(
            "GET",
            "/production-bundles/active",
            actor="reader",
        )
        return _required_mapping(payload, "bundle")

    def _key(self, operation: str) -> str:
        return f"e2e:{self.state.tag}:{operation}"

    def _report(self, **observed: Any) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "state_path": str(self.state_path),
            "tenant_id": self.state.tenant_id,
            "tag": self.state.tag,
            "workflow_id": self.state.workflow_id,
            "analysis_run_id": self.state.analysis_run_id,
            "feedback_case_id": self.state.feedback_case_id,
            "label_id": self.state.label_id,
            "datasets": {
                "golden": self.state.golden_dataset_id,
                "fresh_bad_case": self.state.fresh_dataset_id,
                "high_risk_regression": self.state.high_risk_dataset_id,
            },
            "evaluation_run_id": self.state.evaluation_run_id,
            "candidate_id": self.state.candidate_id,
            "decision": self.state.decision,
            "terminal_phase": self.state.terminal_phase,
            "base_bundle_id": self.state.base_bundle_id,
            "activated_bundle_id": self.state.activated_bundle_id,
            "recovery_workflow_id": self.state.recovery_workflow_id,
            "post_activation_run_id": self.state.post_activation_run_id,
            "rolled_back": self.state.rolled_back,
            "manual_label_gate": self.state.manual_label_gate,
            "label_review_decision": self.state.label_review_decision,
            "label_review_reason": self.state.label_review_reason,
            "actors": self.state.actors,
            "checks": dict(sorted(self.state.checks.items())),
            "observed": observed,
        }


def load_runtime_specs(
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
) -> tuple[ProductionBundleSpec, ProductionBundleSpec]:
    raw = os.getenv("HOT_NEWS_RUNTIME_MANIFEST_JSON")
    try:
        manifest_text = (
            raw if raw is not None and raw.strip() else manifest_path.read_text("utf-8")
        )
        specs = TypeAdapter(tuple[ProductionBundleSpec, ...]).validate_json(
            manifest_text
        )
    except (OSError, ValidationError, ValueError) as exc:
        raise E2EFailure(f"cannot load E2E runtime manifest: {exc}") from exc
    if len(specs) != 2:
        raise E2EFailure("E2E runtime manifest must contain exactly base and candidate")
    base, candidate = specs
    try:
        registry = ProductionBundleRuntimeRegistry(None, specs)
        registry.ensure_transition_supported(
            baseline=base,
            candidate=candidate,
        )
    except UnsupportedProductionBundleRuntimeError as exc:
        raise E2EFailure(f"E2E runtime transition is not executable: {exc}") from exc
    if base == candidate:
        raise E2EFailure("E2E candidate spec must differ from the base spec")
    return base, candidate


def build_structured_diff(
    base: ProductionBundleSpec,
    candidate: ProductionBundleSpec,
) -> list[dict[str, str]]:
    changes = []
    for asset, field_name in ASSET_SPEC_FIELDS.items():
        before = getattr(base, field_name)
        after = getattr(candidate, field_name)
        if before != after:
            changes.append(
                {
                    "asset": asset,
                    "before_version": before,
                    "after_version": after,
                    "reason": f"E2E transition changes {field_name}",
                }
            )
    if not changes:
        raise E2EFailure("candidate does not change any versioned asset")
    return changes


def _required_mapping(values: dict[str, Any], key: str) -> dict[str, Any]:
    value = values.get(key)
    if not isinstance(value, dict):
        raise E2EFailure(f"response is missing object field {key!r}")
    return value


def _required_string(values: dict[str, Any], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise E2EFailure(f"response is missing string field {key!r}")
    return value.strip()


def _print_event(event: str, payload: dict[str, Any]) -> None:
    print(
        json.dumps(
            {"event": event, **payload},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        ),
        flush=True,
    )


def _new_tag() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"{timestamp}-{uuid4().hex[:8]}"


def _validate_tag(value: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,48}", normalized):
        raise E2EFailure("run tag must match [a-z0-9][a-z0-9-]{2,48}")
    return normalized


def _load_or_create_state(args: argparse.Namespace) -> E2EState:
    path = Path(args.state).expanduser().resolve()
    base, candidate = load_runtime_specs(Path(args.manifest).resolve())
    if path.exists():
        state = E2EState.load(path)
        if state.base_spec != base.model_dump(mode="json"):
            raise E2EFailure("state base spec differs from the runtime manifest")
        if state.candidate_spec != candidate.model_dump(mode="json"):
            raise E2EFailure("state candidate spec differs from the runtime manifest")
        if (
            state.decision is not None
            or state.terminal_phase is not None
            or state.rolled_back
        ):
            raise E2EFailure(
                "E2E state already passed the human gate; use decide/recover/verify "
                "as appropriate, or choose a new --state path for a fresh run"
            )
        return state
    tag = _validate_tag(args.run_tag or _new_tag())
    tenant_id = str(UUID(args.tenant_id)) if args.tenant_id else str(uuid4())
    state = E2EState.create(
        tag=tag,
        tenant_id=tenant_id,
        base_spec=base,
        candidate_spec=candidate,
    )
    state.save(path)
    return state


def _load_existing_state(args: argparse.Namespace) -> E2EState:
    path = Path(args.state).expanduser().resolve()
    if not path.exists():
        raise E2EFailure(f"E2E state does not exist: {path}")
    return E2EState.load(path)


def _create_driver(args: argparse.Namespace, state: E2EState) -> DataLoopE2EDriver:
    token = os.getenv("DATA_LOOP_GATEWAY_TOKEN", "")
    state_path = Path(args.state).expanduser().resolve()
    return DataLoopE2EDriver(
        state=state,
        state_path=state_path,
        api=DataLoopApiClient(
            base_url=args.api_url,
            gateway_token=token,
            state=state,
        ),
        hot_news=TemporalHotNewsRunner(
            address=args.temporal_address,
            namespace=args.temporal_namespace,
            task_queue=args.hot_news_task_queue,
            timeout_seconds=args.timeout_seconds,
        ),
        stub_probe=FastGPTStubProbe(args.fastgpt_stub_url),
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.timeout_seconds,
    )


def _human_release_action() -> Literal["approve", "reject"]:
    print(
        "\n人工审批点已到达。请先核对上方 workflow_id、evaluation_run_id、"
        "三层 dataset_id 和角色 UUID。",
        flush=True,
    )
    answer = input("输入 APPROVE 或 REJECT（其他输入不会提交）：").strip().upper()
    if answer == "APPROVE":
        return "approve"
    if answer == "REJECT":
        return "reject"
    raise E2EFailure("human decision aborted; no release decision was submitted")


def _human_label_submission_action() -> Literal["submit"]:
    print(
        "\n人工标签提交点已到达。请核对 feedback_case_id、原始分析"
        "和 labeler UUID。",
        flush=True,
    )
    answer = input(
        "输入 SUBMIT（其他输入不会提交标签）："
    ).strip().upper()
    if answer == "SUBMIT":
        return "submit"
    raise E2EFailure("human label submission aborted; no label was submitted")


def _human_label_review_action() -> Literal["approve", "reject"]:
    print(
        "\n独立标签二审点已到达。请核对 feedback_case_id、label_id、"
        "标签内容以及 label_reviewer UUID。",
        flush=True,
    )
    answer = input(
        "输入 APPROVE 或 REJECT（其他输入不会变更标签）："
    ).strip().upper()
    if answer == "APPROVE":
        return "approve"
    if answer == "REJECT":
        return "reject"
    raise E2EFailure("human label review aborted; no review action was submitted")


def _default_decision_reason(
    action: Literal["approve", "reject"],
    *,
    automated: bool,
) -> str:
    actor = "Automated E2E reviewer" if automated else "E2E release reviewer"
    verb = "approved" if action == "approve" else "rejected"
    return f"{actor} {verb} the candidate after reviewing the gate evidence"


def _default_label_review_reason(
    action: Literal["approve", "reject"],
    *,
    automated: bool,
) -> str:
    actor = "Automated E2E label reviewer" if automated else "E2E label reviewer"
    verb = "approved" if action == "approve" else "rejected"
    return f"{actor} {verb} the submitted feedback label after review"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(command: str) -> argparse.ArgumentParser:
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
        subparser.add_argument(
            "--api-url",
            default=os.getenv("DATA_LOOP_E2E_API_URL", "http://localhost:18000"),
        )
        subparser.add_argument(
            "--temporal-address",
            default=os.getenv("TEMPORAL_ADDRESS", "localhost:17233"),
        )
        subparser.add_argument(
            "--temporal-namespace",
            default=os.getenv("TEMPORAL_NAMESPACE", "default"),
        )
        subparser.add_argument(
            "--hot-news-task-queue",
            default=os.getenv("TEMPORAL_HOT_NEWS_TASK_QUEUE", "hot-news"),
        )
        subparser.add_argument(
            "--fastgpt-stub-url",
            default=os.getenv("FASTGPT_E2E_STUB_URL") or None,
        )
        subparser.add_argument("--poll-seconds", type=float, default=1.0)
        subparser.add_argument("--timeout-seconds", type=float, default=300.0)
        return subparser

    prepare = common("prepare")
    prepare.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH))
    prepare.add_argument("--tenant-id")
    prepare.add_argument("--run-tag")

    prepare_feedback = common("prepare-feedback")
    prepare_feedback.add_argument(
        "--manifest", default=str(DEFAULT_MANIFEST_PATH)
    )
    prepare_feedback.add_argument("--tenant-id")
    prepare_feedback.add_argument("--run-tag")

    label_submit = common("label-submit")
    label_submit.add_argument("--action", choices=("submit",))

    label_review = common("label-review")
    label_review.add_argument("--action", choices=("approve", "reject"))
    label_review.add_argument("--reason", default=None)

    decide = common("decide")
    decide.add_argument("--action", choices=("approve", "reject"))
    decide.add_argument(
        "--reason",
        default=None,
    )
    decide.add_argument("--skip-next-online-run", action="store_true")

    common("verify")

    rollback = common("rollback")
    rollback.add_argument(
        "--reason",
        default="E2E acceptance completed; restore the baseline Bundle",
    )

    recover = common("recover")
    recover.add_argument("--skip-next-online-run", action="store_true")

    auto = common("auto")
    auto.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH))
    auto.add_argument("--tenant-id")
    auto.add_argument("--run-tag")
    auto.add_argument("--action", choices=("approve", "reject"))
    auto.add_argument(
        "--reason",
        default=None,
    )
    auto.add_argument("--skip-next-online-run", action="store_true")
    auto.add_argument("--rollback", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> None:
    if args.poll_seconds <= 0 or args.timeout_seconds <= 0:
        raise E2EFailure("poll and timeout values must be positive")
    state = (
        _load_or_create_state(args)
        if args.command in {"prepare", "prepare-feedback", "auto"}
        else _load_existing_state(args)
    )
    driver = _create_driver(args, state)
    try:
        if args.command == "prepare":
            await driver.prepare()
        elif args.command == "prepare-feedback":
            await driver.prepare_feedback_for_human_label()
        elif args.command == "label-submit":
            args.action or await asyncio.to_thread(_human_label_submission_action)
            await driver.submit_human_label()
        elif args.command == "label-review":
            automated = args.action is not None
            action = args.action or await asyncio.to_thread(
                _human_label_review_action
            )
            await driver.review_human_label(
                action,
                reason=args.reason
                or _default_label_review_reason(action, automated=automated),
            )
        elif args.command == "decide":
            action = args.action or await asyncio.to_thread(
                _human_release_action
            )
            await driver.decide(
                action,
                reason=args.reason
                or _default_decision_reason(action, automated=False),
                verify_next_online_run=not args.skip_next_online_run,
            )
        elif args.command == "verify":
            await driver.verify()
        elif args.command == "rollback":
            await driver.rollback(reason=args.reason)
        elif args.command == "recover":
            await driver.recover_activation(
                verify_next_online_run=not args.skip_next_online_run,
            )
        elif args.command == "auto":
            await driver.prepare()
            action = args.action or await asyncio.to_thread(
                _human_release_action
            )
            await driver.decide(
                action,
                reason=args.reason
                or _default_decision_reason(action, automated=True),
                verify_next_online_run=not args.skip_next_online_run,
            )
            await driver.verify()
            if args.rollback and action == "approve":
                await driver.rollback(
                    reason="Automated E2E acceptance completed; restore baseline"
                )
                await driver.verify()
        else:  # pragma: no cover - argparse enforces this
            raise E2EFailure(f"unsupported command: {args.command}")
    finally:
        await driver.close()


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(_run(args))
    except (E2EFailure, httpx.HTTPError) as exc:
        raise SystemExit(f"Data Loop E2E failed: {exc}") from exc


if __name__ == "__main__":
    main()
