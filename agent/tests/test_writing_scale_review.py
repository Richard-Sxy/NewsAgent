from copy import deepcopy
import hashlib
import json
from pathlib import Path
import pytest
from evaluation import prepare_writing_scale_review as review
from evaluation.manual_editing_server import EditingStore


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def pack(tmp_path, monkeypatch):
    dataset, run = tmp_path / "dataset", tmp_path / "run"
    dataset.mkdir()
    run.mkdir()
    rows = [{"case_id": "C" + str(i), "event_group_id": "E" + str(i), "split": "evaluation",
             "input": {"topic": "任务" + str(i), "requirements": {
                 "task_description": "依据原文写短稿", "source_documents": [
                     {"source_id": "S", "title": "报道", "url": "https://news.example/" + str(i),
                      "content": "材料正文" + str(i)}]}}} for i in range(11)]
    (dataset / "inputs.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    put(dataset / "private/gold-review.draft.json", {"status": "draft", "secret_answer": "NEVER_SENT"})
    put(dataset / "editing-subset.json", {"schema_version": 1,
        "selection": "pre_generation", "case_ids": [r["case_id"] for r in rows[:10]]})
    put(run / "plan.json", {"planned_tasks": 11})
    for row in rows:
        for arm in ("baseline", "candidate"):
            directory = run / "cases" / row["case_id"] / arm
            directory.mkdir(parents=True)
            raw = ("稿件" + row["case_id"] + arm).encode()
            (directory / "draft.md").write_bytes(raw)
            put(directory / "metadata.json", {"status": "completed",
                "draft_sha256": hashlib.sha256(raw).hexdigest()})
    monkeypatch.setattr(review, "verify", lambda path, dataset_override: ({"gold_reference": {"sha256": review.digest(dataset / "private/gold-review.draft.json")}, "editing_subset_sha256": review.digest(dataset / "editing-subset.json")}, deepcopy(rows)))
    monkeypatch.setattr(review, "status", lambda path, dataset_override: {
        "planned_tasks": 11, "planned_requests": 22, "completed_requests": 22, "requests": []})
    return dataset, run, rows, tmp_path / "export"


def test_review_nulls_and_counterbalanced_editing_without_started_timers(pack):
    dataset, run, rows, out = pack
    result = review.export_review(run, dataset, out)
    assert result["planned_requests"] == 22
    form = review.read(out / "review-template.json")
    assert len(form["records"]) == 22
    assert all(r["critical_error"] is None and r["reviewer"] is None for r in form["records"])
    package = review.read(out / "editing-tasks.json")
    assert len(package["tasks"]) == 20
    assert sum(t["arm"] == "baseline" for t in package["tasks"][:10]) == 5
    assert package["gold_status"] == "draft"
    assert "NEVER_SENT" not in (out / "editing-tasks.json").read_text()
    store = EditingStore(out / "editing-tasks.json", out / "session")
    assert all(t["status"] == "not_started" for t in store.listing()["tasks"])


def test_failed_selected_pair_is_kept_and_not_replaced_by_complete_spare(pack):
    dataset, run, rows, out = pack
    put(run / "cases/C0/candidate/metadata.json", {"status": "failed", "error_type": "length"})
    result = review.export_review(run, dataset, out)
    assert not result["editing_pairs_ready"]
    assert not (out / "editing-tasks.json").exists()
    receipt = review.read(out / "editing-readiness.json")
    assert receipt["completed_selected_drafts"] == 19
    assert "C10" not in receipt["pre_registered_case_ids"]
    records = review.read(out / "review-template.json")["records"]
    failed = next(r for r in records if r["case_id"] == "C0" and r["arm"] == "candidate")
    assert failed["generation_status"] == "failed" and failed["critical_error"] is None


def test_completed_draft_tampering_blocks_export_before_output(pack):
    dataset, run, rows, out = pack
    (run / "cases/C0/candidate/draft.md").write_text("tampered")
    with pytest.raises(ValueError, match="hash"):
        review.export_review(run, dataset, out)
    assert not out.exists()


def test_changed_dataset_cannot_be_relabelled_as_frozen_input(pack):
    dataset, run, rows, out = pack
    changed = deepcopy(rows)
    changed[0]["input"]["topic"] = "changed"
    (dataset / "inputs.jsonl").write_text("\n".join(json.dumps(r) for r in changed))
    with pytest.raises(ValueError, match="matches frozen"):
        review.export_review(run, dataset, out)
    assert not out.exists()


def test_subset_requires_ten_distinct_registered_tasks(pack):
    dataset, run, rows, out = pack
    put(dataset / "editing-subset.json", {"case_ids": ["C0"] * 10})
    with pytest.raises(ValueError, match="unique"):
        review.export_review(run, dataset, out)


def test_original_draft_gold_not_silently_converted_to_approval(pack):
    dataset, run, rows, out = pack
    put(dataset / "private/gold-review.draft.json", {"status": "approved"})
    with pytest.raises(ValueError, match="draft gold"):
        review.export_review(run, dataset, out)


def test_existing_review_is_preserved(pack):
    dataset, run, rows, out = pack
    out.mkdir()
    (out / "user-notes.txt").write_text("keep")
    with pytest.raises(ValueError, match="output exists"):
        review.export_review(run, dataset, out)
    assert (out / "user-notes.txt").read_text() == "keep"
