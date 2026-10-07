"""Aggregate Port acceptance is bounded, private and never production approval."""

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, Inexact, localcontext
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from app.analytics.entities import ContentType
from app.analytics.metric_source import HotNewsMetricQuery
from app.analytics.metrics import NewsMetricSnapshot
from app.data_analysis.acceptance import AggregateContractError, AcceptanceRequest, run_aggregate_acceptance
from tools import check_analysis_data_contract as cli


TENANT = "11111111-1111-4111-8111-111111111111"
START = datetime(2026, 10, 3, 2, tzinfo=timezone.utc)


def query(**changes):
    return replace(HotNewsMetricQuery(
        tenant_id=TENANT, window_start=START, window_end=START + timedelta(hours=1),
        content_types=frozenset({ContentType.ARTICLE, ContentType.VIDEO}),
    ), **changes)


def snapshot(**changes):
    return replace(NewsMetricSnapshot(
        news_id="private-news-identifier", content_type=ContentType.ARTICLE,
        window_start=START, window_end=START + timedelta(hours=1),
        impressions=100, clicks=10, unique_users=7, total_duration_seconds=140,
        effective_consumptions=7, interactions=3, ctr=Decimal("0.1"),
    ), **changes)


class Source:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch_snapshots(self, current):
        self.calls.append(current)
        return self.rows


@pytest.mark.asyncio
async def test_acceptance_forwards_trusted_query_and_retains_no_row_details():
    source = Source([snapshot(), snapshot(news_id="another", content_type=ContentType.VIDEO)])
    current = query()
    result = await run_aggregate_acceptance(source, current)
    assert source.calls == [current]
    assert source.calls[0].tenant_id == TENANT
    assert result["status"] == "local_contract_passed"
    assert result["counts"] == {
        "returned_row_count": 2, "validated_row_count": 2,
        "content_type_rows": {"article": 1, "video": 1},
    }
    assert len(result["evidence"]["aggregate_snapshot_sha256"]) == 64
    assert len(result["evidence"]["tenant_scope_sha256"]) == 64
    assert {"name": "tenant_isolation", "status": "not_verified", "code": "response_has_no_tenant_field"} in result["checks"]
    serialized = json.dumps(result, allow_nan=False)
    for private in (TENANT, "private-news-identifier", "another", "unique_users", "impressions", "user_id"):
        assert private not in serialized
    assert "production acceptance is not established" in serialized
    assert "No automatic retry" in serialized


@pytest.mark.asyncio
async def test_adapter_contract_status_does_not_claim_production_or_isolation():
    result = await run_aggregate_acceptance(Source([snapshot()]), query(), mode="adapter_contract")
    assert result["status"] == "contract_passed"
    assert result["acceptance_kind"] == "adapter_contract"
    assert result["checks"][-1]["status"] == "not_verified"


@pytest.mark.asyncio
@pytest.mark.parametrize("rows,code", [
    ([{"user_id": "SECRET-RAW-USER", "event_id": "SECRET-RAW-EVENT"}], "aggregate_snapshot_type_required"),
    ((snapshot(),), "aggregate_list_required"),
    ([snapshot(), snapshot()], "aggregate_identity_duplicate"),
    ([snapshot(content_type="article")], "aggregate_content_type_scope_mismatch"),
    ([snapshot(window_end=START + timedelta(hours=2))], "aggregate_window_scope_mismatch"),
    ([snapshot(window_start=START.replace(tzinfo=None))], "window_invalid"),
    ([snapshot(news_id="")], "aggregate_news_identity_invalid"),
    ([snapshot(news_id="control\nSECRET")], "aggregate_news_identity_invalid"),
    ([snapshot(news_id="x" * 129)], "aggregate_news_identity_invalid"),
    ([snapshot(impressions=True)], "aggregate_count_invalid"),
    ([snapshot(clicks=Decimal("10"))], "aggregate_count_invalid"),
    ([snapshot(unique_users=-1)], "aggregate_count_invalid"),
    ([snapshot(interactions=2**63)], "aggregate_count_invalid"),
    ([snapshot(ctr=0.1)], "aggregate_ctr_invalid"),
    ([snapshot(ctr=Decimal("NaN"))], "aggregate_ctr_invalid"),
    ([snapshot(ctr=Decimal("Infinity"))], "aggregate_ctr_invalid"),
    ([snapshot(ctr=Decimal("-0.1"))], "aggregate_ctr_invalid"),
    ([snapshot(ctr=Decimal("1e-100000"))], "aggregate_ctr_invalid"),
    ([snapshot(ctr=Decimal("0.2"))], "aggregate_ctr_inconsistent"),
])
async def test_bad_aggregate_contract_is_safe_and_single_attempt(rows, code):
    source = Source(rows)
    result = await run_aggregate_acceptance(source, query())
    assert result["status"] == "contract_failed"
    assert result["checks"][-1]["code"] == code
    assert result["counts"]["validated_row_count"] == 0
    assert result["evidence"]["aggregate_snapshot_sha256"] is None
    assert len(source.calls) == 1
    serialized = json.dumps(result, allow_nan=False)
    assert "SECRET" not in serialized
    assert "private-news-identifier" not in serialized


@pytest.mark.asyncio
async def test_snapshot_subclass_with_extra_user_data_is_rejected():
    class ExtendedSnapshot(NewsMetricSnapshot):
        pass
    raw = ExtendedSnapshot(**{field: getattr(snapshot(), field) for field in snapshot().__dataclass_fields__})
    object.__setattr__(raw, "raw_user_id", "SECRET-EXTRA-USER")
    result = await run_aggregate_acceptance(Source([raw]), query())
    assert result["checks"][-1]["code"] == "aggregate_snapshot_type_required"
    assert "SECRET" not in json.dumps(result)


@pytest.mark.asyncio
async def test_content_type_filter_and_row_limit_are_enforced():
    article_query = query(content_types=frozenset({ContentType.ARTICLE}))
    result = await run_aggregate_acceptance(Source([snapshot(content_type=ContentType.VIDEO)]), article_query)
    assert result["checks"][-1]["code"] == "aggregate_content_type_scope_mismatch"
    source = Source([snapshot(), snapshot(news_id="second")])
    result = await run_aggregate_acceptance(source, query(), max_rows=1)
    assert result["checks"][-1]["code"] == "aggregate_row_budget_exceeded"
    assert source.calls == [query()]


@pytest.mark.asyncio
async def test_ctr_rounding_and_zero_impressions_follow_existing_contract():
    rows = [snapshot(impressions=3, clicks=1, ctr=Decimal("0.3333")),
            snapshot(news_id="zero", impressions=0, clicks=2, ctr=Decimal("-0"))]
    result = await run_aggregate_acceptance(Source(rows), query())
    assert result["status"] == "local_contract_passed"
    # The existing Port permits repeated click/event semantics. Validation
    # requires correct CTR without imposing unconfirmed counter inequalities.
    result = await run_aggregate_acceptance(Source([snapshot(impressions=2, clicks=3, ctr=Decimal("1.5"))]), query())
    assert result["status"] == "local_contract_passed"


@pytest.mark.asyncio
async def test_evidence_hash_is_canonical_and_changes_with_aggregate_or_tenant():
    rows = [snapshot(), snapshot(news_id="second", content_type=ContentType.VIDEO)]
    first = await run_aggregate_acceptance(Source(rows), query())
    offset = timezone(timedelta(hours=8))
    equivalent = query(window_start=START.astimezone(offset), window_end=(START + timedelta(hours=1)).astimezone(offset))
    normalized = await run_aggregate_acceptance(Source([replace(row, ctr=Decimal("0.1000")) for row in reversed(rows)]), equivalent)
    assert first["evidence"] == normalized["evidence"]
    different = await run_aggregate_acceptance(Source([snapshot(interactions=4), rows[1]]), query())
    assert first["evidence"]["aggregate_snapshot_sha256"] != different["evidence"]["aggregate_snapshot_sha256"]
    second_tenant = await run_aggregate_acceptance(Source(rows), query(tenant_id="22222222-2222-4222-8222-222222222222"))
    assert first["evidence"]["tenant_scope_sha256"] != second_tenant["evidence"]["tenant_scope_sha256"]
    assert first["evidence"]["aggregate_snapshot_sha256"] != second_tenant["evidence"]["aggregate_snapshot_sha256"]


@pytest.mark.asyncio
async def test_decimal_validation_ignores_callers_small_context():
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        result = await run_aggregate_acceptance(Source([snapshot(impressions=3, clicks=1, ctr=Decimal("0.3333"))]), query())
        assert context.prec == 2
    assert result["status"] == "local_contract_passed"


@pytest.mark.asyncio
async def test_empty_response_is_explicitly_inconclusive():
    result = await run_aggregate_acceptance(Source([]), query())
    assert result["status"] == "local_contract_passed"
    assert result["counts"]["validated_row_count"] == 0
    assert any("response was empty" in text for text in result["limitations"])


@pytest.mark.asyncio
async def test_external_exception_is_never_echoed_or_retried():
    class Broken:
        calls = 0
        async def fetch_snapshots(self, current):
            self.calls += 1
            raise RuntimeError("SECRET-TOKEN https://internal.example/user/RAW")
    source = Broken()
    result = await run_aggregate_acceptance(source, query())
    assert source.calls == 1
    assert result["checks"][-1]["code"] == "source_failure"
    assert "SECRET" not in json.dumps(result)
    assert "internal.example" not in json.dumps(result)


@pytest.mark.asyncio
async def test_custom_response_timezone_exception_cannot_escape_safe_code():
    class MaliciousZone(tzinfo):
        def utcoffset(self, current):
            raise AggregateContractError("SECRET-TIMEZONE-TOKEN")
    row = snapshot(window_start=START.replace(tzinfo=MaliciousZone()))
    result = await run_aggregate_acceptance(Source([row]), query())
    assert result["checks"][-1]["code"] == "aggregate_validation_failed"
    assert "SECRET" not in json.dumps(result)


@pytest.mark.asyncio
async def test_timeout_cancels_one_attempt_and_retains_no_old_results():
    class Slow:
        calls = 0
        cancelled = False
        async def fetch_snapshots(self, current):
            self.calls += 1
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
    source = Slow()
    result = await run_aggregate_acceptance(source, query(), timeout_seconds=0.1)
    assert source.calls == 1
    assert source.cancelled
    assert result["checks"][-1]["code"] == "source_timeout"
    assert result["evidence"]["aggregate_snapshot_sha256"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("updates,kwargs", [
    ({"tenant_id": "SECRET-INVALID-TENANT"}, {}),
    ({"window_start": START.replace(tzinfo=None)}, {}),
    ({"window_end": START}, {}),
    ({"content_types": frozenset({"video"})}, {}),
    ({"requested_metrics": frozenset({"raw_user_id"})}, {}),
    ({"ranking_limit": True}, {}),
    ({}, {"max_rows": True}),
    ({}, {"max_rows": 1001}),
    ({}, {"timeout_seconds": float("nan")}),
    ({}, {"timeout_seconds": 0.01}),
    ({}, {"timeout_seconds": 10**1000}),
    ({}, {"mode": "productionaccepted"}),
])
async def test_invalid_trusted_scope_is_rejected_before_call(updates, kwargs):
    source = Source([snapshot()])
    result = await run_aggregate_acceptance(source, query(**updates), **kwargs)
    assert source.calls == []
    assert result["checks"][-1]["code"] == "request_invalid"
    assert "SECRET" not in json.dumps(result, allow_nan=False)


def config():
    return {
        "schema_version": "1.0", "tenant_id": TENANT,
        "window_start": START.isoformat(), "window_end": (START + timedelta(hours=1)).isoformat(),
        "content_types": ["article", "video"], "max_rows": 200,
        "timeout_seconds": 3, "source_factory": None,
    }


def test_public_config_exact_whitelist_and_request_projection(tmp_path):
    path = tmp_path / "request.yml"
    path.write_text(yaml.safe_dump(config()))
    request, factory = cli.load_config(str(path))
    assert factory is None
    assert isinstance(request, AcceptanceRequest)
    assert request.to_query().tenant_id == TENANT
    assert request.to_query().ranking_limit == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"api_key": "SECRET-CONFIG-KEY"}, {"password": "SECRET-CONFIG-PASSWORD"},
    {"source_factory": "x:run();bad"}, {"schema_version": 1.0},
    {"content_types": ["article", "article"]}, {"content_types": []},
    {"timeout_seconds": True}, {"tenant_id": "bad"},
])
async def test_cli_config_rejection_never_echoes_private_values(tmp_path, changes):
    path = tmp_path / "request.yml"
    path.write_text(yaml.safe_dump({**config(), **changes}))
    result = await cli.run_cli(argparse.Namespace(config=str(path), factory=None))
    assert result["status"] == "contract_failed"
    assert result["checks"][0]["code"] == "configuration_or_factory_invalid"
    assert "SECRET" not in json.dumps(result)


def test_duplicate_yaml_keys_and_env_file_are_rejected(tmp_path, monkeypatch):
    path = tmp_path / "duplicate.yml"
    path.write_text(yaml.safe_dump(config()) + "max_rows: 100\n")
    with pytest.raises(cli.AcceptanceConfigError):
        cli.load_config(str(path))
    env = tmp_path / ".env"
    env.write_text("SECRET=not-read")
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: pytest.fail(".env was read"))
    with pytest.raises(cli.AcceptanceConfigError):
        cli.load_config(str(env))


@pytest.mark.asyncio
async def test_factory_requires_explicit_selection_and_handles_bad_contract(monkeypatch):
    calls = []
    def factory(name):
        calls.append(name)
        return Source([{"raw_user_id": "SECRET"}])
    monkeypatch.setattr(cli, "load_source_factory", factory)
    local = await cli.run_cli(argparse.Namespace(config=None, factory=None))
    assert local["status"] == "local_contract_passed"
    assert calls == []
    bad = await cli.run_cli(argparse.Namespace(config=None, factory="fixture:source"))
    assert calls == ["fixture:source"]
    assert bad["status"] == "contract_failed"
    assert bad["acceptance_kind"] == "adapter_contract"
    assert "SECRET" not in json.dumps(bad)


@pytest.mark.asyncio
async def test_explicit_factory_loader_errors_are_safe(monkeypatch):
    def broken(name):
        raise RuntimeError("SECRET-FACTORY-TOKEN")
    monkeypatch.setattr(cli, "load_source_factory", broken)
    result = await cli.run_cli(argparse.Namespace(config=None, factory="fixture:source"))
    assert result["status"] == "contract_failed"
    assert "SECRET" not in json.dumps(result)


def test_default_cli_uses_only_synthetic_data_and_json_exit_status():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "tools/check_analysis_data_contract.py"], cwd=root,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["status"] == "local_contract_passed"
    assert report["acceptance_kind"] == "synthetic_fixture"
    assert TENANT not in result.stdout
    assert "synthetic-article" not in result.stdout
    assert result.stderr == ""
