"""Synthetic offline contracts for source snapshots and reused writing baselines."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path

import pytest

from evaluation import run_writing_snapshot_ab as runner


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def reseal(out, plan):
    save(out / "plan.json", plan)
    (out / "plan.sha256").write_text(runner.base.sha(out / "plan.json") + "\n")


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    before, out = tmp_path / "synthetic-v1", tmp_path / "synthetic-v2"
    before.mkdir()
    rows = [{"case_id": case, "event_group_id": event, "split": "development",
             "input": {"topic": "synthetic public task", "requirements": {
                 "task_description": "Only synthetic public sources",
                 "source_documents": [{"source_id": "S", "url": "https://example.com/source", "content": "public synthetic source"}]}}}
            for case, event in (("case-a", "event-a"), ("case-b", "event-b"))]
    old_plan = {"gold_reference": {"path": "private/gold.json",
                                  "sha256": "a" * 64, "private_sentinel": "DO_NOT_SEND_PRIVATE_GOLD"}}
    save(before / "plan.json", old_plan)
    save(before / "execution.json", {"completed_requests": 4})
    for row in rows:
        folder = before / "cases" / row["case_id"] / "baseline"
        folder.mkdir(parents=True)
        (folder / "draft.md").write_text("synthetic original baseline " + row["case_id"])
        save(folder / "metadata.json", {"case_id": row["case_id"], "status": "completed",
             "model": runner.base.MODEL_ID, "finish_reason": "stop", "request_attempts": 1,
             "draft_sha256": runner.base.sha(folder / "draft.md")})
    monkeypatch.setattr(runner.base, "verify", lambda path: (old_plan, rows))
    monkeypatch.setattr(runner, "sources", lambda: {"synthetic-source": "b" * 64})
    monkeypatch.setattr(runner, "build_metric_snapshot",
                        lambda value: {"public_metric": "synthetic Decimal result", "public_input_sha256":
                                       hashlib.sha256(runner.base.canonical(value)).hexdigest()})
    runner.prepare(before, out)
    requests = []
    monkeypatch.setattr(runner.base, "fetch_health", lambda: {"status": "ok", "model": runner.base.MODEL_ID})
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            return json.dumps({"model": runner.base.MODEL_ID, "choices": [
                {"message": {"role": "assistant", "content": "synthetic generated draft"},
                 "finish_reason": "stop"}], "usage": {"total_tokens": 5}}).encode()
    def urlopen(request, timeout):
        requests.append((json.loads(request.data), timeout))
        return Response()
    monkeypatch.setattr(runner.urllib.request, "urlopen", urlopen)
    return before, out, rows, requests


def test_payload_contains_public_sources_and_snapshot_never_private_gold(prepared):
    before, out, rows, requests = prepared
    original = {str(p.relative_to(before)): p.read_bytes() for p in before.rglob("*") if p.is_file()}
    result = runner.run(out)
    assert result["attempted_candidate_calls"] == 2
    assert result["completed_candidate_drafts"] == 2
    assert len(requests) == 2
    for body, timeout in requests:
        assert body["model"] == runner.base.MODEL_ID
        assert body["max_tokens"] == 2048 and body["temperature"] == 0 and timeout == 300
        user = json.loads(body["messages"][1]["content"])
        assert set(user) == {"public_input", "computed_metric_snapshot"}
        assert "source_documents" in user["public_input"]["requirements"]
        assert "DO_NOT_SEND_PRIVATE_GOLD" not in json.dumps(body)
        assert "gold_reference" not in json.dumps(body)
    assert original == {str(p.relative_to(before)): p.read_bytes() for p in before.rglob("*") if p.is_file()}


def test_reused_baselines_preserve_bytes_and_are_not_counted_as_new_calls(prepared):
    before, out, rows, requests = prepared
    runner.run(out)
    for row in rows:
        source = before / "cases" / row["case_id"] / "baseline" / "draft.md"
        target = out / "cases" / row["case_id"] / "baseline"
        assert source.read_bytes() == (target / "draft.md").read_bytes()
        metadata = runner.base.read(target / "metadata.json")
        assert metadata["draft_sha256"] == runner.base.sha(source)
        assert metadata["new_model_calls"] == 0
        assert metadata["reused_from"] == str(source)
    assert len(requests) == 2


def test_repeated_run_does_not_issue_more_model_requests(prepared):
    _, out, _, requests = prepared
    runner.run(out)
    with pytest.raises((ValueError, FileExistsError)):
        runner.run(out)
    assert len(requests) == 2


@pytest.mark.parametrize("filename", ["inputs.json", "metric-snapshots.json"])
def test_frozen_public_input_or_snapshot_drift_blocks_before_calls(prepared, filename):
    _, out, _, requests = prepared
    (out / filename).write_text("{}")
    with pytest.raises(ValueError, match="frozen input"):
        runner.run(out)
    assert not requests


def test_source_drift_blocks_before_calls(prepared, monkeypatch):
    _, out, _, requests = prepared
    monkeypatch.setattr(runner, "sources", lambda: {"changed-source": "c" * 64})
    with pytest.raises(ValueError, match="source"):
        runner.run(out)
    assert not requests


def test_second_baseline_drift_is_checked_before_any_candidate_call(prepared):
    before, out, rows, requests = prepared
    (before / "cases" / rows[1]["case_id"] / "baseline" / "draft.md").write_text("changed")
    with pytest.raises(ValueError):
        runner.run(out)
    assert not requests
    assert not (out / "run-start.json").exists()


def test_original_baseline_plan_drift_blocks_before_calls(prepared):
    before, out, _, requests = prepared
    (before / "plan.json").write_text("{}")
    with pytest.raises(ValueError):
        runner.run(out)
    assert not requests


@pytest.mark.parametrize("field,value", [
    ("model", "other-model"), ("revision", "other-revision"),
    ("url", "https://example.com/inference"), ("max_tokens", 8192),
    ("temperature", 1), ("timeout_seconds", 1), ("automatic_retries", 1),
    ("planned_tasks", 3), ("planned_candidate_calls", 3), ("reused_baseline_drafts", 3),
])
def test_profile_or_budget_mismatch_is_rejected_even_with_fresh_seal(prepared, field, value):
    _, out, _, requests = prepared
    plan = runner.base.read(out / "plan.json")
    plan[field] = value
    reseal(out, plan)
    with pytest.raises(ValueError):
        runner.run(out)
    assert not requests


def test_duplicate_case_or_snapshot_membership_is_rejected_before_calls(prepared):
    _, out, rows, requests = prepared
    save(out / "inputs.json", [rows[0], rows[0]])
    plan = runner.base.read(out / "plan.json")
    plan["inputs_sha256"] = runner.base.sha(out / "inputs.json")
    reseal(out, plan)
    with pytest.raises(ValueError):
        runner.run(out)
    assert not requests
