import json
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from app.clients.fastgpt import FastGPTClient
from app.services.agents.hot_news import HotNewsAnalysisAgentRunner
from app.services.hot_news_analysis import HotNewsAnalysisValidator
from examples.data_loop_e2e import (
    DEFAULT_MANIFEST_PATH,
    DataLoopE2EDriver,
    E2EFailure,
    E2EState,
    _default_decision_reason,
    _default_label_review_reason,
    _human_label_review_action,
    _human_label_submission_action,
    build_parser,
    build_structured_diff,
    load_runtime_specs,
)
from examples.fastgpt_e2e_stub import app as fastgpt_stub_app
from examples.hot_news_e2e_support import build_hot_news_e2e_dependencies
from tests.test_analysis_feedback_schema import analysis_input


def test_e2e_manifest_is_an_executable_external_profile_transition(
    monkeypatch,
) -> None:
    monkeypatch.delenv("HOT_NEWS_RUNTIME_MANIFEST_JSON", raising=False)

    base, candidate = load_runtime_specs(DEFAULT_MANIFEST_PATH)
    changes = build_structured_diff(base, candidate)

    assert {item["asset"] for item in changes} == {
        "analysis_prompt",
        "fastgpt_app",
        "model",
    }
    assert base.metric_definition_version == candidate.metric_definition_version
    assert base.fastgpt_app_id != candidate.fastgpt_app_id


def test_e2e_state_uses_distinct_human_identities_and_never_serializes_token(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("HOT_NEWS_RUNTIME_MANIFEST_JSON", raising=False)
    monkeypatch.setenv("DATA_LOOP_GATEWAY_TOKEN", "must-never-be-persisted")
    base, candidate = load_runtime_specs(DEFAULT_MANIFEST_PATH)
    state = E2EState.create(
        tag="unit-e2e",
        tenant_id="11111111-1111-4111-8111-111111111111",
        base_spec=base,
        candidate_spec=candidate,
    )
    path = tmp_path / "state.json"

    state.save(path)
    restored = E2EState.load(path)
    serialized = path.read_text(encoding="utf-8")

    assert restored.tenant_id == state.tenant_id
    assert len({UUID(value) for value in restored.actors.values()}) == len(
        restored.actors
    )
    assert "must-never-be-persisted" not in serialized
    assert json.loads(serialized)["checks"] == {}


def test_reject_reason_is_not_recorded_as_an_acceptance() -> None:
    reason = _default_decision_reason("reject", automated=True)

    assert "rejected" in reason
    assert "accepted" not in reason


def test_manual_label_cli_exposes_three_resumable_stages() -> None:
    parser = build_parser()

    prepare = parser.parse_args(
        ["prepare-feedback", "--state", "/tmp/manual-label.json"]
    )
    submit = parser.parse_args(
        [
            "label-submit",
            "--state",
            "/tmp/manual-label.json",
            "--action",
            "submit",
        ]
    )
    review = parser.parse_args(
        [
            "label-review",
            "--state",
            "/tmp/manual-label.json",
            "--action",
            "reject",
        ]
    )

    assert prepare.command == "prepare-feedback"
    assert submit.action == "submit"
    assert review.action == "reject"
    assert "rejected" in _default_label_review_reason(
        "reject", automated=False
    )


def test_manual_label_prompts_require_literal_actions(monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "SUBMIT")
    assert _human_label_submission_action() == "submit"
    monkeypatch.setattr("builtins.input", lambda _: "APPROVE")
    assert _human_label_review_action() == "approve"
    monkeypatch.setattr("builtins.input", lambda _: "not-approved")
    with pytest.raises(E2EFailure, match="no review action"):
        _human_label_review_action()


class _PendingLabelApi:
    def __init__(self, state: E2EState, *, status: str = "pending") -> None:
        self.state = state
        self.status = status
        self.requests: list[tuple[str, str]] = []

    async def preflight(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def request(self, method, path, **kwargs):
        self.requests.append((method, path))
        if method == "GET" and path.endswith("/labels"):
            return {
                "labels": [
                    {
                        "id": self.state.label_id,
                        "label_version": self.state.label_version,
                        "approval_status": self.status,
                        "labeled_by": self.state.actors["labeler"],
                        "approved_by": (
                            self.state.actors["label_reviewer"]
                            if self.status == "approved"
                            else None
                        ),
                        "approved_at": (
                            "2026-09-11T01:00:00+00:00"
                            if self.status == "approved"
                            else None
                        ),
                    }
                ]
            }
        if method == "POST" and path.endswith("/review"):
            assert kwargs["actor"] == "label_reviewer"
            body = kwargs["json_body"]
            assert body["action"] == "request_changes"
            self.status = "rejected"
            return {
                "label": {
                    "id": self.state.label_id,
                    "label_version": self.state.label_version,
                    "approval_status": "rejected",
                    "labeled_by": self.state.actors["labeler"],
                    "approved_by": None,
                    "reviewed_by": self.state.actors["label_reviewer"],
                    "review_reason": body["reason"],
                },
                "created": True,
            }
        raise AssertionError(f"unexpected mutating API request: {method} {path}")


@pytest.mark.asyncio
async def test_manual_label_rejection_is_resumable_and_never_approves(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("HOT_NEWS_RUNTIME_MANIFEST_JSON", raising=False)
    base, candidate = load_runtime_specs(DEFAULT_MANIFEST_PATH)
    state = E2EState.create(
        tag="manual-label-reject",
        tenant_id="11111111-1111-4111-8111-111111111111",
        base_spec=base,
        candidate_spec=candidate,
    )
    state.manual_label_gate = True
    state.feedback_case_id = "22222222-2222-4222-8222-222222222222"
    state.label_id = "33333333-3333-4333-8333-333333333333"
    state.label_version = 1
    api = _PendingLabelApi(state)
    state_path = tmp_path / "manual-label.json"
    driver = DataLoopE2EDriver(
        state=state,
        state_path=state_path,
        api=api,
        hot_news=SimpleNamespace(),
        stub_probe=SimpleNamespace(),
        poll_seconds=0.01,
        timeout_seconds=0.1,
    )

    report = await driver.review_human_label(
        "reject",
        reason="independent reviewer found the label unsuitable",
    )

    assert state.label_review_decision == "reject"
    assert report["label_review_decision"] == "reject"
    assert api.requests == [
        (
            "GET",
            f"/feedback-cases/{state.feedback_case_id}/labels",
        ),
        (
            "POST",
            f"/feedback-cases/{state.feedback_case_id}/labels/{state.label_id}/review",
        ),
    ]
    restored = E2EState.load(state_path)
    assert restored.label_review_decision == "reject"
    with pytest.raises(E2EFailure, match="cannot continue"):
        await driver._submit_and_approve_label()


@pytest.mark.asyncio
async def test_manual_label_approval_recovers_after_api_success_before_checkpoint(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("HOT_NEWS_RUNTIME_MANIFEST_JSON", raising=False)
    base, candidate = load_runtime_specs(DEFAULT_MANIFEST_PATH)
    state = E2EState.create(
        tag="manual-label-recover",
        tenant_id="11111111-1111-4111-8111-111111111111",
        base_spec=base,
        candidate_spec=candidate,
    )
    state.manual_label_gate = True
    state.feedback_case_id = "22222222-2222-4222-8222-222222222222"
    state.label_id = "33333333-3333-4333-8333-333333333333"
    state.label_version = 1
    api = _PendingLabelApi(state, status="approved")
    driver = DataLoopE2EDriver(
        state=state,
        state_path=tmp_path / "manual-label-approved.json",
        api=api,
        hot_news=SimpleNamespace(),
        stub_probe=SimpleNamespace(),
        poll_seconds=0.01,
        timeout_seconds=0.1,
    )

    await driver.review_human_label(
        "approve",
        reason="retry after the durable approval response was lost",
    )
    await driver._submit_and_approve_label()

    assert state.label_review_decision == "approve"
    assert state.feedback_window_end == "2026-09-11T01:00:00+00:00"
    assert all(method == "GET" for method, _ in api.requests)


@pytest.mark.asyncio
async def test_recovery_driver_never_overrides_a_deliberate_rollback(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("HOT_NEWS_RUNTIME_MANIFEST_JSON", raising=False)
    base, candidate = load_runtime_specs(DEFAULT_MANIFEST_PATH)
    state = E2EState.create(
        tag="rollback-guard",
        tenant_id="11111111-1111-4111-8111-111111111111",
        base_spec=base,
        candidate_spec=candidate,
    )
    state.workflow_id = "workflow-1"
    state.candidate_id = "22222222-2222-4222-8222-222222222222"
    state.base_bundle_id = "33333333-3333-4333-8333-333333333333"
    state.decision = "approve"
    state.terminal_phase = "activated"
    state.rolled_back = True
    driver = DataLoopE2EDriver(
        state=state,
        state_path=tmp_path / "state.json",
        api=SimpleNamespace(),
        hot_news=SimpleNamespace(),
        stub_probe=SimpleNamespace(),
        poll_seconds=0.01,
        timeout_seconds=0.1,
    )

    with pytest.raises(E2EFailure, match="cannot override"):
        await driver.recover_activation()


@pytest.mark.asyncio
async def test_scenario_baseline_accepts_the_activated_candidate_version() -> None:
    dependencies = build_hot_news_e2e_dependencies(
        tenant_id="11111111-1111-4111-8111-111111111111",
        production_bundle_version="candidate-v2",
    )

    baselines = await dependencies.baseline_provider.get_baselines(
        tenant_id=dependencies.tenant_id,
        window_start=dependencies.window_start,
        window_end=dependencies.window_end,
        production_bundle_version="candidate-v2",
        metric_keys=frozenset(),
    )

    assert baselines == {}
    assert dependencies.production_bundle_version == "candidate-v2"
    assert dependencies.news_ids


@pytest.mark.asyncio
async def test_fastgpt_e2e_stub_satisfies_real_client_and_business_validator() -> None:
    transport = httpx.ASGITransport(app=fastgpt_stub_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://fastgpt-e2e",
    ) as http_client:
        await http_client.post("/e2e/reset")
        client = FastGPTClient(
            SimpleNamespace(
                fastgpt_base_url="http://fastgpt-e2e",
                fastgpt_api_key="e2e-key",
            ),
            http_client=http_client,
        )
        trusted_input = analysis_input()
        result = await HotNewsAnalysisAgentRunner(
            client,
            "e2e-hot-news-app-v2",
        ).run(trusted_input)
        HotNewsAnalysisValidator().validate(
            analysis_input=trusted_input,
            result=result,
        )
        call_log = (await http_client.get("/e2e/calls")).json()["calls"]

    assert result.value.news_id == trusted_input.news_id
    assert result.request_id is not None
    assert [item["app_id"] for item in call_log] == ["e2e-hot-news-app-v2"]
