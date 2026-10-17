"""Only indexed aggregate rows are queried; no event or user table exists."""
import asyncio
from hashlib import sha256
import json
import sqlite3
import threading

import pytest

from app.reader_profiles import build
from app.reader_profiles.contracts import ReaderProfileProviderError, ReaderProfileQuery
from app.reader_profiles.local import LocalNewsReaderProfileClient
from tests.test_reader_profile_build import OTHER, TENANT, event, get_row, seal, stamp, write_source


@pytest.fixture
def package(tmp_path):
    source = write_source(tmp_path / "source")
    target = tmp_path / "reader"
    build.build_package(source, target)
    return target


def query(client, *, news=("A",), tenant=TENANT, hour=50, **changes):
    return ReaderProfileQuery(tenant_id=tenant, news_ids=news, window_start=stamp(hour), window_end=stamp(hour + 1),
        source_release_sha256=changes.pop("source_release_sha256", client.source_release_sha256), **changes)


def test_async_client_returns_strict_batch_and_preserves_identity(package):
    client = LocalNewsReaderProfileClient(package, environment="test")
    result = asyncio.run(client.fetch(query(client)))
    assert str(result.tenant_id) == TENANT and result.items[0].reader_count == 3
    assert result.source_release_sha256 == sha256((package / "manifest.json").read_bytes()).hexdigest()
    assert result.items[0].source_mode == "synthetic"
    assert result.items[0].profile_policy_version == client.expected_profile_policy_version
    assert result.items[0].profile_cutoff == result.window_start and result.watermark >= result.window_end
    assert client.catalog.taxonomy_version == client.expected_taxonomy_version
    assert result.items[0].long_term.topics[0].lift is None
    assert any("long-term history is truncated" in note for note in result.items[0].limitations)
    assert not any("short-term history is truncated" in note for note in result.items[0].limitations)


def test_known_unvisited_news_is_empty_only_in_covered_window(package):
    client = LocalNewsReaderProfileClient(package, environment="local")
    result = asyncio.run(client.fetch(query(client, news=("B",))))
    for item in result.items:
        assert item.reader_count == 0 and item.status == "empty"
        assert item.long_term.topics == () and item.long_term.baseline_profiled_count == 0
        assert "empty reader cohort; baseline not computed" in item.limitations
    assert result.items[0].content_sha256 is not None
    assert not any("history is truncated" in note for note in result.items[0].limitations)
    with pytest.raises(ReaderProfileProviderError) as failure:
        asyncio.run(client.fetch(query(client, hour=72)))
    assert failure.value.code == "invalid_query"


def test_unknown_content_cannot_be_reported_as_zero_readers(package):
    client = LocalNewsReaderProfileClient(package, environment="test")
    with pytest.raises(ReaderProfileProviderError) as failure:
        asyncio.run(client.fetch(query(client, news=("unknown-news",))))
    assert failure.value.code == "invalid_query"


def test_early_history_is_flagged_but_complete_final_history_is_not(tmp_path):
    rows = [event("u", 0, "profile_initialized"), event("u", .5, "click", "B"),
        event("u", 1.2, "detail_view", "A"), event("u", 740, "click", "C"),
        event("u", 743.2, "detail_view", "A")]
    source = write_source(tmp_path / "source", rows, end_hours=744)
    package = tmp_path / "reader"
    build.build_package(source, package)
    client = LocalNewsReaderProfileClient(package, environment="test")
    early = asyncio.run(client.fetch(query(client, hour=1))).items[0]
    final = asyncio.run(client.fetch(query(client, hour=743))).items[0]
    assert sum("history is truncated" in note for note in early.limitations) == 2
    assert not any("history is truncated" in note for note in final.limitations)
    assert client.identity_sha256 != client.source_release_sha256


def test_sql_injection_is_rejected_and_tenants_are_isolated(package):
    client = LocalNewsReaderProfileClient(package, environment="test")
    attack = "A' OR 1=1--"
    with pytest.raises(ReaderProfileProviderError) as failure:
        asyncio.run(client.fetch(query(client, news=(attack,))))
    assert failure.value.code == "invalid_query"
    result = asyncio.run(client.fetch(query(client, tenant=OTHER)))
    assert result.items[0].reader_count == 1


def test_database_work_runs_off_event_loop(package, monkeypatch):
    client = LocalNewsReaderProfileClient(package, environment="test")
    original = client._fetch_sync
    worker_ids = []

    def wrapped(value):
        worker_ids.append(threading.get_ident())
        return original(value)

    monkeypatch.setattr(client, "_fetch_sync", wrapped)
    asyncio.run(client.fetch(query(client)))
    assert worker_ids and worker_ids[0] != threading.get_ident()


def test_closed_client_rejects_calls(package):
    client = LocalNewsReaderProfileClient(package, environment="test")
    asyncio.run(client.close())
    with pytest.raises(ReaderProfileProviderError) as failure:
        asyncio.run(client.fetch(query(client)))
    assert failure.value.code == "closed"


def test_source_release_mismatch_rejected(package):
    client = LocalNewsReaderProfileClient(package, environment="test")
    with pytest.raises(ReaderProfileProviderError) as failure:
        asyncio.run(client.fetch(query(client, source_release_sha256="f" * 64)))
    assert failure.value.code == "invalid_query"


def test_production_disallows_synthetic(package):
    with pytest.raises(ValueError):
        LocalNewsReaderProfileClient(package, environment="production")


@pytest.mark.parametrize("name", ["policy.json", "reader_profiles.sqlite"])
def test_artifact_tampering_detected(package, name):
    path = package / name
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="hash mismatch"):
        LocalNewsReaderProfileClient(package, environment="test")


def reseal_sqlite(package):
    manifest = json.loads((package / "manifest.json").read_text())
    path = package / "reader_profiles.sqlite"
    manifest["artifacts"][path.name] = dict(bytes=path.stat().st_size, sha256=build._hash_file(path))
    seal(package, manifest)


def test_missing_coverage_does_not_become_zero_readers(package):
    with sqlite3.connect(package / "reader_profiles.sqlite") as connection:
        connection.execute("DELETE FROM coverage WHERE tenant_id=? AND window_start=?", (TENANT, stamp(50)))
    reseal_sqlite(package)
    client = LocalNewsReaderProfileClient(package, environment="test")
    with pytest.raises(ReaderProfileProviderError) as failure:
        asyncio.run(client.fetch(query(client)))
    assert failure.value.code == "invalid_query"


def test_resealed_payload_identity_mismatch_fails(package):
    payload = get_row(package)
    payload["news_id"] = "different-news"
    with sqlite3.connect(package / "reader_profiles.sqlite") as connection:
        connection.execute("UPDATE reader_profiles SET payload_json=? WHERE tenant_id=? AND news_id=? AND window_start=?",
            (build._json(payload), TENANT, "A", stamp(50)))
    reseal_sqlite(package)
    client = LocalNewsReaderProfileClient(package, environment="test")
    with pytest.raises(ReaderProfileProviderError) as failure:
        asyncio.run(client.fetch(query(client)))
    assert failure.value.code == "invalid_response"
