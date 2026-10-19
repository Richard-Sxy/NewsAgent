"""Exercise the offline security runner's evidence and predictor boundaries."""

import json
from pathlib import Path

import pytest

from evaluation import run_text2sql_security as runner


def public_input(tmp_path: Path, records: list[dict], **identity_overrides):
    path = tmp_path / "test.jsonl"
    raw = "".join(json.dumps(row) + "\n" for row in records).encode()
    path.write_bytes(raw)
    manifest_path = tmp_path / "source-manifest.json"
    manifest = {
        "revision": "frozen-test-revision",
        "evaluation_input": {
            "path": path.name,
            "sha256": runner.digest(raw),
            "rows": len(records),
            "split": "test",
            **identity_overrides,
        },
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, manifest_path, manifest


def test_public_loader_preserves_frozen_rows_and_both_file_identities(tmp_path):
    expected = [{"question": "新闻点击前五条", "malicious": 0}]
    path, manifest_path, manifest = public_input(tmp_path, expected)

    rows, identities = runner.load_public_rows(path, manifest_path)

    assert rows == expected
    assert identities == [
        {"path": str(path.resolve()), "sha256": runner.digest(path.read_bytes()), "rows": 1},
        {
            "path": str(manifest_path.resolve()),
            "sha256": runner.digest(manifest_path.read_bytes()),
            "source": manifest,
        },
    ]


@pytest.mark.parametrize(
    "override",
    [{"sha256": "0" * 64}, {"rows": 2}, {"split": "train"}],
    ids=["wrong-hash", "wrong-denominator", "wrong-split"],
)
def test_public_loader_rejects_manifest_identity_drift(tmp_path, override):
    path, manifest_path, _ = public_input(
        tmp_path, [{"question": "新闻点击前五条", "malicious": 0}], **override
    )

    with pytest.raises(ValueError, match="evaluation_input identity mismatch"):
        runner.load_public_rows(path, manifest_path)


def test_public_loader_rejects_changed_input_after_freezing(tmp_path):
    path, manifest_path, _ = public_input(
        tmp_path, [{"question": "新闻点击前五条", "malicious": 0}]
    )
    path.write_text('{"question":"changed","malicious":0}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="evaluation_input identity mismatch"):
        runner.load_public_rows(path, manifest_path)


@pytest.mark.parametrize("label", [True, False, "1", 2, -1, None, 0.0])
def test_public_loader_rejects_unknown_and_noninteger_labels(tmp_path, label):
    path, manifest_path, _ = public_input(
        tmp_path, [{"question": "新闻点击前五条", "malicious": label}]
    )

    with pytest.raises(ValueError, match="invalid or unknown public label/question"):
        runner.load_public_rows(path, manifest_path)


@pytest.mark.parametrize("question", [None, 123, ["news"]])
def test_public_loader_rejects_nontext_questions(tmp_path, question):
    path, manifest_path, _ = public_input(
        tmp_path, [{"question": question, "malicious": 0}]
    )

    with pytest.raises(ValueError, match="invalid or unknown public label/question"):
        runner.load_public_rows(path, manifest_path)


def test_public_loader_requires_a_nonempty_split_and_paired_manifest(tmp_path):
    path, manifest_path, _ = public_input(tmp_path, [])

    with pytest.raises(ValueError, match="1..10000 rows"):
        runner.load_public_rows(path, manifest_path)
    with pytest.raises(ValueError, match="requires a source manifest"):
        runner.load_public_rows(path, None)
    with pytest.raises(ValueError, match="manifest without SQLShield input"):
        runner.load_public_rows(None, manifest_path)
    assert runner.load_public_rows(None, None) == ([], [])


@pytest.mark.parametrize("blocked", [False, True], ids=["allow", "block"])
def test_screen_predictor_receives_only_question_and_reads_label_after_prediction(
    monkeypatch, blocked
):
    calls = []

    class AuditedRow(dict):
        predicted = False

        def __getitem__(self, key):
            if key in {"query", "context"}:
                raise AssertionError("SQL and schema must not reach question prediction")
            if key == "malicious":
                assert self.predicted, "gold label read before the prediction completed"
            return super().__getitem__(key)

    for label in (0, 1):
        row = AuditedRow(
            question="same question", malicious=label, query=object(), context=object()
        )

        def predictor(question):
            assert not row.predicted
            assert question == "same question"
            calls.append(question)
            row.predicted = True
            if blocked:
                raise runner.SqlAssistantQuestionError("synthetic screen rejection")

        monkeypatch.setattr(runner, "screen_question", predictor)
        result = runner.screening_probe(row, label)

        assert result["observed_blocked"] is blocked
        assert result["malicious"] == label
        assert result["model_calls"] == 0
        assert result["sql_executed"] is False
        assert result["suite"] == "sqlshield_question_screen_only"

    assert calls == ["same question", "same question"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attack,authorized_setup_calls",
    [
        ("other_tenant", 0),
        ("other_user", 0),
        ("cached_other_user", 1),
        ("tamper_sql", 0),
        ("tamper_tenant", 0),
        ("tamper_limit", 0),
        ("tamper_metric_rehash", 0),
    ],
)
async def test_service_probe_blocks_scope_and_rehashed_snapshot_attacks_before_port(
    attack, authorized_setup_calls
):
    result = await runner.service_probe(
        {"id": f"test-{attack}", "attack": attack, "expected": "block"}
    )

    assert result["observed"] == "block", result
    assert result["passed"] is True
    assert result["error"]["type"] == "SqlAssistantError"
    assert result["warehouse_stub_calls"] == authorized_setup_calls
    assert result["warehouse_stub_calls_after_attack"] == 0
    assert result["model_stub_calls"] == 1
    assert result["query_id"]
    assert result["sql_executed"] is False
    assert result["otel_trace_id"] is None


@pytest.mark.asyncio
async def test_service_probe_positive_control_reaches_recording_port_and_names_stubs():
    result = await runner.service_probe(
        {"id": "test-control", "attack": "control", "expected": "allow"}
    )

    assert result["passed"] is True, result
    assert result["observed"] == "allow"
    assert result["warehouse_stub_calls_after_attack"] == 1
    assert result["error"] is None
    stages = {row["name"]: row for row in result["stages"]}
    assert stages["warehouse_execute"]["actual_component"] == "RecordingWarehouse"
    assert stages["result_saved"]["actual_component"] == "MemorySnapshots"
    assert result["sql_executed"] is False


def test_public_confusion_counts_retain_false_negatives_without_claiming_acceptance():
    records = [
        {
            "suite": "sqlshield_question_screen_only",
            "observed_blocked": blocked,
            "malicious": label,
        }
        for blocked, label in [(True, 1), (False, 1), (True, 0), (False, 0)]
    ]

    summary = runner.summarize(records)["sqlshield_question_screen_only"]

    assert summary["count"] == 4
    assert {key: summary[key] for key in ("tp", "fn", "fp", "tn")} == {
        "tp": 1,
        "fn": 1,
        "fp": 1,
        "tn": 1,
    }
    assert summary["status"] == "exploratory_not_acceptance"
    assert summary["malicious_question_block_rate"] == 0.5
    assert summary["benign_question_block_rate"] == 0.5
