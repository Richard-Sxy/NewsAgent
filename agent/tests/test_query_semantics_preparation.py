"""Preparation contracts: no inference, training or human approval is simulated."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from evaluation import run_query_semantics as cli
from evaluation.query_semantics_common import (load_package, read_jsonl, validate_cases,
    validate_gold, write_jsonl)

SEED = Path(__file__).resolve().parents[1] / "evaluation/datasets/business_sql/query_semantics_v1"


@pytest.fixture
def package(tmp_path):
    out = tmp_path / "package"
    result = cli.prepare(SEED / "cases.jsonl", SEED / "labels.jsonl", "development", out)
    assert result["planned"] == 24
    return out


def test_freeze_is_draft_and_labels_are_separate(package):
    manifest, cases, gold = load_package(package)
    assert manifest["review_status"] == "draft"
    assert len(cases) == len(gold) == 24
    assert all("target" not in c and "human_review" not in c for c in cases)
    assert manifest["cohorts"] == {"golden": 18, "fresh": 0, "high_risk": 6}


def test_public_validation_does_not_open_gold(package):
    (package / "private/labels.jsonl").unlink()
    assert len(load_package(package, with_gold=False)[1]) == 24
    with pytest.raises(OSError):
        load_package(package)


@pytest.mark.parametrize("filename", ["inputs.jsonl", "runtime.json", "schema.json", "private/labels.jsonl"])
def test_asset_drift_blocks(package, filename):
    with (package / filename).open("a") as f:
        f.write(" ")
    with pytest.raises(ValueError):
        load_package(package)


def test_no_overwrite(package):
    with pytest.raises(FileExistsError):
        cli.prepare(SEED / "cases.jsonl", SEED / "labels.jsonl", "development", package)


def test_same_group_and_question_cannot_cross_splits():
    case = read_jsonl(SEED / "cases.jsonl")[0]
    other = deepcopy(case)
    other.update(id="duplicate", split="test")
    with pytest.raises(ValueError, match="group crosses"):
        validate_cases([case, other])
    other["group_id"] = "renamed"
    with pytest.raises(ValueError, match="question crosses"):
        validate_cases([case, other])


def test_synthetic_cases_cannot_be_called_fresh():
    case = read_jsonl(SEED / "cases.jsonl")[0]
    case["cohort"] = "fresh"
    with pytest.raises(ValueError, match="actual reviewed"):
        validate_cases([case])


def test_gold_cannot_self_approve():
    case, label = read_jsonl(SEED / "cases.jsonl")[0], read_jsonl(SEED / "labels.jsonl")[0]
    label["human_review"] = {"status": "approved", "annotator": "person", "reviewer": "person"}
    with pytest.raises(ValueError, match="self-approve"):
        validate_gold([case], [label])


def test_gold_cannot_exclude_hard_ready_cases_from_main():
    case, label = read_jsonl(SEED / "cases.jsonl")[0], read_jsonl(SEED / "labels.jsonl")[0]
    label["main_em"] = False
    with pytest.raises(ValueError, match="all ready"):
        validate_gold([case], [label])


def test_draft_training_export_is_blocked(package, tmp_path):
    case, label = read_jsonl(SEED / "cases.jsonl")[0], read_jsonl(SEED / "labels.jsonl")[0]
    case.update(id="train-1", group_id="new-train", split="train", question="查询默认窗口新闻点击最多的前五条。")
    label["id"] = case["id"]
    write_jsonl(tmp_path / "train-inputs.jsonl", [case])
    write_jsonl(tmp_path / "train-gold.jsonl", [label])
    with pytest.raises(ValueError, match="human review"):
        cli.export_train(tmp_path / "train-inputs.jsonl", tmp_path / "train-gold.jsonl",
                         SEED / "cases.jsonl", package, tmp_path / "train-export")


def test_training_messages_bind_prompt_and_assistant_target(package, tmp_path):
    case, label = read_jsonl(SEED / "cases.jsonl")[0], read_jsonl(SEED / "labels.jsonl")[0]
    case.update(id="unit-train-1", group_id="unit-train-group", split="train",
                question="查询默认窗口新闻点击最多的前五条。")
    label["id"] = case["id"]
    # Unit-fixture identities, never exported to the user's actual dataset.
    label["human_review"] = {"status": "approved", "annotator": "fixture-annotator", "reviewer": "fixture-reviewer"}
    write_jsonl(tmp_path / "inputs.jsonl", [case])
    write_jsonl(tmp_path / "labels.jsonl", [label])
    out = tmp_path / "training"
    summary = cli.export_train(tmp_path / "inputs.jsonl", tmp_path / "labels.jsonl",
                               SEED / "cases.jsonl", package, out)
    messages = read_jsonl(out / "train.messages.jsonl")[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant"]
    assert json.loads(messages[1]["content"])["question"] == case["question"]
    assert "target" not in json.loads(messages[1]["content"])
    assert json.loads(messages[2]["content"]) == label["target"]
    assert summary["training_started"] is False


def test_portable_bundle_runs_without_repository_or_gold(package, tmp_path):
    archive = tmp_path / "kit.zip"
    result = cli.bundle(package, archive)
    assert not result["gold_included"]
    with zipfile.ZipFile(archive) as z:
        assert not any("private/" in name or ".env" in name for name in z.namelist())
        z.extractall(tmp_path / "unpacked")
    completed = subprocess.run([sys.executable, "-m", "evaluation.run_query_semantics", "validate",
                                "--package", "package", "--public-only"],
                               cwd=tmp_path / "unpacked", capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["model_quality"] == "not_run"
