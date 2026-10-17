"""The runnable replay preserves evidence and actually reopens persisted state."""

import json
from pathlib import Path
import socket

import pytest
import yaml

from tools.replay_tier_lifecycle import (
    DEFAULT_CONFIG,
    LocalReplayDatabase,
    build_runtime,
    load_knowledge_lifecycle_config,
    main,
    replay,
)


@pytest.mark.asyncio
async def test_persistent_replay_covers_tiers_updates_withdrawal_and_restart(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("local replay must never use network")
    monkeypatch.setattr(socket, "create_connection", no_network)
    output = tmp_path / "replay"
    report = await replay(config_path=DEFAULT_CONFIG, output_dir=output)
    assert report["status"] == "passed"
    assert all(check["passed"] for check in report["checks"])
    checks = {check["name"] for check in report["checks"]}
    assert {"one_seed_article_in_each_tier", "old_revision_hidden_before_reindex",
            "pending_job_and_news_survive_restart", "reheated_news_has_one_hot_result",
            "seed_hot_aged_to_warm", "seed_hot_aged_to_cold", "withdraw_gate_is_immediate",
            "withdrawn_news_stays_absent_after_restart", "tenant_isolation"} <= checks
    assert report["final_active_news_counts"] == {"hot": 0, "warm": 0, "cold": 2}
    assert json.loads((output / "report.json").read_text()) == report
    assert (output / "lifecycle.sqlite3").stat().st_size > 0
    for step in report["steps"]:
        assert step["trace"]["total_ms"] >= 0
        assert set(step["trace"]["tiers"]) == {"hot", "warm", "cold"}
    assert report["measurement_scope"]["ann_performance"] is False
    assert report["measurement_scope"]["temporal_dispatch_exercised"] is False

    # A further independent reopen validates the delivered SQLite artifact,
    # instead of trusting only the replay's own in-process assertions.
    database = LocalReplayDatabase(output / "lifecycle.sqlite3")
    try:
        runtime = build_runtime(database, load_knowledge_lifecycle_config(DEFAULT_CONFIG))
        row = await runtime.ledger.get_news(tenant_id=report["tenant_id"], news_id="seed-cold")
        assert row["status"] == "retracted"
        assert row["source_revision"] == 3
        assert row["active_job_id"] is None
        result = await runtime.search.search(query_text="合成新闻", tenant_id=report["tenant_id"], limit=3)
        assert {hit.news_id for hit in result["hits"]} == {"seed-hot", "seed-warm"}
    finally:
        database.close()


@pytest.mark.asyncio
async def test_existing_evidence_is_not_overwritten(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    evidence = output / "user-evidence.txt"
    evidence.write_text("keep this")
    with pytest.raises(ValueError, match="never overwritten"):
        await replay(config_path=DEFAULT_CONFIG, output_dir=output)
    assert evidence.read_text() == "keep this"
    assert list(output.iterdir()) == [evidence]


@pytest.mark.asyncio
async def test_enterprise_model_cannot_be_mislabelled_as_local_simulation(tmp_path):
    values = yaml.safe_load(DEFAULT_CONFIG.read_text())
    values["embedding_version"] = "enterprise-model-v1"
    config = tmp_path / "custom.yml"
    config.write_text(yaml.safe_dump(values))
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="native-local-embedding-v1"):
        await replay(config_path=config, output_dir=output)
    assert not output.exists()


def test_cli_success_and_second_run_refusal(tmp_path, capsys):
    output = tmp_path / "cli"
    assert main(["--output-dir", str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "passed"
    before = (output / "report.json").read_bytes()
    assert main(["--output-dir", str(output)]) == 1
    assert "never overwritten" in capsys.readouterr().err
    assert (output / "report.json").read_bytes() == before
