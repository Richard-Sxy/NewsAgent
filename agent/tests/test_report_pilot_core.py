"""Report pilot contracts use native Runners and an isolated local inference Port."""
from copy import deepcopy
from dataclasses import replace
import asyncio
import json
from pathlib import Path

import pytest

from app.model_runtime.agent_client import NativeStructuredAgentClient
from app.model_runtime.config_file import load_model_runtime_config
from app.model_runtime.core import RawInferenceResult, StructuredInferenceService
from app.model_runtime.http import ModelTransportError
from app.model_runtime.local_inference import LocalHotNewsInference
from evaluation.report_pilot_core import RunBudget, run_report_case


CONFIG = Path(__file__).resolve().parents[1] / "deploy/model-runtime.local.yml"


def report_case(case_id="CASE-1"):
    return {
        "case_id": case_id, "event_group_id": case_id + "-EVENT", "split": "development",
        "input": {"topic": "测试资料报告", "requirements": {
            "max_sections": 2, "as_of": "2026-10-24T00:00:00+08:00",
            "source_documents": [
                {"url": f"https://example.org/newsagent-local/{index}", "content": "隔离测试资料。"}
                for index in (1, 2)
            ],
        }},
        "gold": {"do_not_send": "PRIVATE_GOLD_SENTINEL"},
    }


class RecordingLocalPort:
    def __init__(self, mutate=None, failure=None, delay=0.0):
        self.inner = LocalHotNewsInference()
        self.mutate = mutate
        self.failure = failure
        self.delay = delay
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure:
            raise self.failure
        raw = await self.inner.complete(request)
        if self.mutate:
            payload = json.loads(raw.content)
            self.mutate(request.scene, payload)
            raw = replace(raw, content=json.dumps(payload, ensure_ascii=False))
        return replace(raw, usage={"total_tokens": 10, "unsafe_payload": "MUST_NOT_PERSIST", "bool_token": True})


def client_for(port):
    config = load_model_runtime_config(CONFIG)
    return NativeStructuredAgentClient(
        StructuredInferenceService(inference=port, prompts=config.prompt_registry()), config,
    )


def all_files_text(path):
    return "\n".join(p.read_text() for p in path.iterdir() if p.is_file())


@pytest.mark.asyncio
async def test_native_first_pass_stops_after_reviewer_and_preserves_safe_artifacts(tmp_path):
    port = RecordingLocalPort()
    dest = tmp_path / "run"
    trace = await run_report_case(report_case(), client_for(port), dest)
    assert trace["status"] == "completed"
    assert trace["review_decision"] == "human_review"
    assert trace["model_calls"] == 5
    assert [request.scene for request in port.requests] == ["research", "outline", "section", "assemble", "review"]
    assert trace == json.loads((dest / "execution.json").read_text())
    assert all(stage["status"] == "completed" for stage in trace["stages"])
    assert trace["outputs"]["review"]["scores"]["factuality"] == 0
    assert trace["quality_status"] == "pending_human_review"
    text = all_files_text(dest)
    assert "raw_content" not in text
    assert "MUST_NOT_PERSIST" not in text
    assert "PRIVATE_GOLD_SENTINEL" not in text
    for request in port.requests:
        assert "PRIVATE_GOLD_SENTINEL" not in request.messages[1].content
    assert all(stage["raw_sha256"] and stage["raw_bytes"] > 0 for stage in trace["stages"])
    assert all(stage["usage"] == {"total_tokens": 10} for stage in trace["stages"])


@pytest.mark.asyncio
async def test_every_outline_section_is_written_before_assembly(tmp_path):
    def two_sections(scene, payload):
        if scene == "outline":
            second = deepcopy(payload["sections"][0])
            second.update(section_id="S02", order=2)
            payload["sections"].append(second)
    port = RecordingLocalPort(mutate=two_sections)
    trace = await run_report_case(report_case(), client_for(port), tmp_path / "two")
    assert trace["status"] == "completed"
    assert trace["model_calls"] == 6
    assert [section["section_id"] for section in trace["outputs"]["sections"]] == ["S01", "S02"]
    assert trace["outputs"]["draft"]["section_ids"] == ["S01", "S02"]
    second_input = json.loads(port.requests[3].messages[1].content)
    assert list(second_input["previous_sections_summary"]) == ["S01"]


@pytest.mark.asyncio
async def test_reviewer_rewrite_is_preserved_without_automatic_revision(tmp_path):
    def rewrite(scene, payload):
        if scene == "review":
            payload["decision"] = "rewrite"
            payload["issues"][0].update(suggested_action="rewrite", section_id="S01")
            payload["section_decisions"] = [{"section_id": "S01", "decision": "rewrite", "issue_ids": ["I001"]}]
    port = RecordingLocalPort(mutate=rewrite)
    trace = await run_report_case(report_case(), client_for(port), tmp_path / "rewrite")
    assert trace["status"] == "completed"
    assert trace["review_decision"] == "rewrite"
    assert trace["model_calls"] == 5
    assert all(request.scene != "revise" for request in port.requests)
    assert trace["human_approval"] is False


@pytest.mark.asyncio
async def test_research_business_quality_failure_retains_output_and_blocks_dependencies(tmp_path):
    def insufficient(scene, payload):
        if scene == "research":
            payload["facts"] = payload["facts"][:1]
            payload["suggested_angles"] = []
    port = RecordingLocalPort(mutate=insufficient)
    trace = await run_report_case(report_case(), client_for(port), tmp_path / "low")
    assert trace["status"] == "failed"
    assert trace["model_calls"] == 1
    assert trace["stages"][0]["error_type"] == "AgentOutputValidationError"
    assert trace["outputs"]["research"]["metrics"]["fact_count"] == 1
    assert all(stage["status"] == "not_run" for stage in trace["stages"][1:])


@pytest.mark.asyncio
async def test_research_rejects_source_outside_input_whitelist(tmp_path):
    def unknown_source(scene, payload):
        if scene == "research":
            payload["facts"][0]["source_url"] = "https://not-in-input.example/"
    port = RecordingLocalPort(mutate=unknown_source)
    trace = await run_report_case(report_case(), client_for(port), tmp_path / "unknown")
    assert trace["status"] == "failed"
    assert trace["model_calls"] == 1
    assert trace["stages"][0]["error_code"] == "unknown_source_url"


@pytest.mark.asyncio
async def test_overlong_outline_is_rejected_before_section_calls(tmp_path):
    def excessive(scene, payload):
        if scene == "outline":
            payload["sections"] = [
                {**payload["sections"][0], "section_id": f"S{index:02d}", "order": index}
                for index in range(1, 4)
            ]
    port = RecordingLocalPort(mutate=excessive)
    trace = await run_report_case(report_case(), client_for(port), tmp_path / "long")
    assert trace["status"] == "failed"
    assert trace["model_calls"] == 2
    assert trace["stages"][1]["error_code"] == "outline_section_limit_exceeded"
    assert not trace["outputs"]["sections"]


@pytest.mark.asyncio
async def test_budget_is_shared_across_cases_and_counts_attempts(tmp_path):
    port = RecordingLocalPort()
    client = client_for(port)
    budget = RunBudget(max_calls=6, max_wall_seconds=30)
    first = await run_report_case(report_case("A"), client, tmp_path / "a", budget=budget)
    second = await run_report_case(report_case("B"), client, tmp_path / "b", budget=budget)
    assert first["status"] == "completed"
    assert second["status"] == "blocked"
    assert second["model_calls"] == 1
    assert second["stages"][1]["reason"] == "call_budget_exhausted"
    assert budget.attempts == len(port.requests) == 6


@pytest.mark.asyncio
async def test_outer_deadline_stops_port_that_ignores_request_timeout(tmp_path):
    port = RecordingLocalPort(delay=0.1)
    budget = RunBudget(max_calls=6, max_wall_seconds=0.01)
    trace = await run_report_case(report_case(), client_for(port), tmp_path / "timeout", budget=budget)
    assert trace["status"] == "blocked"
    assert trace["model_calls"] == 1
    assert trace["stages"][0]["error_type"] == "TimeoutError"
    assert trace["error_code"] == "wall_budget_exhausted"
    assert all(stage["status"] == "not_run" for stage in trace["stages"][1:])


@pytest.mark.asyncio
async def test_errors_never_dump_exception_message_or_invalid_raw_response(tmp_path):
    class InvalidJsonPort:
        async def complete(self, request):
            return RawInferenceResult(content="RAW_RESPONSE_SECRET_SENTINEL", request_id="request-1",
                                      model_version=request.model_route, usage={})
    dest = tmp_path / "invalid"
    trace = await run_report_case(report_case(), client_for(InvalidJsonPort()), dest)
    assert trace["status"] == "failed"
    assert trace["stages"][0]["raw_bytes"] == len("RAW_RESPONSE_SECRET_SENTINEL")
    assert "RAW_RESPONSE_SECRET_SENTINEL" not in all_files_text(dest)
    error_dest = tmp_path / "error"
    port = RecordingLocalPort(failure=RuntimeError("AUTH_SECRET_SENTINEL"))
    trace = await run_report_case(report_case(), client_for(port), error_dest)
    assert trace["model_calls"] == 1
    assert trace["stages"][0]["error_type"] == "RuntimeError"
    assert "AUTH_SECRET_SENTINEL" not in all_files_text(error_dest)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage,failure_kind", [
    ("model_binding", "model_version_mismatch"),
    ("model_binding", "unapproved_model"),
    ("inference", "http_error"),
    ("inference", "rate_limit"),
    ("inference", "connection"),
])
async def test_provider_identity_and_dependency_failures_block_quality_scoring(
    tmp_path, failure_stage, failure_kind,
):
    error = ModelTransportError(
        "HTTP_AUTH_BODY_SECRET", retryable=False, request_id="request-dependency",
        failure_stage=failure_stage, failure_kind=failure_kind,
    )
    port = RecordingLocalPort(failure=error)
    dest = tmp_path / "dependency"
    trace = await run_report_case(report_case(), client_for(port), dest)
    assert trace["status"] == "blocked"
    assert trace["model_calls"] == 1
    assert trace["failure_stage"] == failure_stage
    assert trace["failure_kind"] == trace["error_code"] == failure_kind
    assert trace["stages"][0]["status"] == "failed"
    assert trace["stages"][0]["request_id"] == "request-dependency"
    assert trace["failed_stage_count"] == 1
    assert trace["not_run_stage_count"] == 4
    assert "HTTP_AUTH_BODY_SECRET" not in all_files_text(dest)


@pytest.mark.asyncio
async def test_provider_timeout_remains_a_failed_attempt_in_the_denominator(tmp_path):
    error = ModelTransportError("safe timeout", retryable=True, failure_kind="timeout")
    port = RecordingLocalPort(failure=error)
    trace = await run_report_case(report_case(), client_for(port), tmp_path / "provider-timeout")
    assert trace["status"] == "failed"
    assert trace["model_calls"] == trace["failed_stage_count"] == 1
    assert trace["stages"][0]["failure_kind"] == "timeout"


@pytest.mark.asyncio
async def test_missing_second_source_blocks_before_inference(tmp_path):
    case = report_case()
    case["input"]["requirements"]["source_documents"] = case["input"]["requirements"]["source_documents"][:1]
    port = RecordingLocalPort()
    trace = await run_report_case(case, client_for(port), tmp_path / "blocked")
    assert trace["status"] == "blocked"
    assert trace["error_code"] == "missing_second_source"
    assert trace["model_calls"] == 0
    assert not port.requests


@pytest.mark.asyncio
async def test_reusing_output_directory_is_rejected_without_any_call(tmp_path):
    dest = tmp_path / "existing"
    dest.mkdir()
    (dest / "keep.txt").write_text("keep")
    port = RecordingLocalPort()
    with pytest.raises(FileExistsError):
        await run_report_case(report_case(), client_for(port), dest)
    assert not port.requests
    assert (dest / "keep.txt").read_text() == "keep"


@pytest.mark.parametrize("kwargs", [
    {"max_calls": -1, "max_wall_seconds": 1},
    {"max_calls": True, "max_wall_seconds": 1},
    {"max_calls": 1, "max_wall_seconds": float("inf")},
    {"max_calls": 1, "max_wall_seconds": 0},
    {"max_calls": 1, "max_wall_seconds": 1, "max_sections": 0},
])
def test_invalid_budget_is_rejected(kwargs):
    with pytest.raises(ValueError):
        RunBudget(**kwargs)
