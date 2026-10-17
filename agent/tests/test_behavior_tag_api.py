"""Isolated label API: gateway identity, source isolation and aggregate-only output."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.behavior_tags import router
from app.api.dependencies import get_data_loop_gateway_token
from app.audience.behavior_package import BehaviorPackageAudienceClient
from app.audience.contracts import AudienceProviderError, AudienceQuery, AudienceSelection
from app.behavior_tags.runtime import LocalBehaviorRuntime
from app.behavior_tags.tagging import load_topic_policy


TENANT = "11111111-1111-4111-8111-111111111111"
OTHER = "33333333-3333-4333-8333-333333333333"
USER = "22222222-2222-4222-8222-222222222222"
START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = START + timedelta(hours=2)
VERSION = "synthetic-interest-profile-v1"
POLICY = Path(__file__).resolve().parents[1] / "deploy/behavior-topic-policy.local.yml"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@pytest.fixture
def index(tmp_path):
    policy = load_topic_policy(POLICY)
    content = [dict(tenant_id=tenant, news_id="same-news", title="科技研究" if tenant == TENANT else "其他租户专属",
        content_sha256="a" * 64, topic_ids=["tech", "tech.nature"], topic_scores={"tech": 1.0},
        taxonomy_version=policy.catalog.taxonomy_version, tagging_policy_version=policy.policy_version,
        matches=[], provenance="rule_inferred") for tenant in (TENANT, OTHER)]
    observations = []
    for tenant, dataset, scenario, definition, hour, clicks in (
        (TENANT, "dataset-v8", "scene-v8", "metrics-v8", 0, 0),
        (TENANT, "dataset-v6", "scene-v6", "metrics-v6", 0, 2),
        (TENANT, "dataset-v8", "scene-v8", "metrics-v8", 1, 7),
        (OTHER, "dataset-v8", "scene-v8", "metrics-v8", 0, 999),
    ):
        observations.append(dict(tenant_id=tenant, news_id="same-news",
            window_start=(START + timedelta(hours=hour)).isoformat(),
            window_end=(START + timedelta(hours=hour + 1)).isoformat(),
            source_dataset_version=dataset, scenario_id=scenario, metric_definition_id=definition,
            clicks=clicks, impressions=0 if clicks == 0 else 1000, unique_users=0 if clicks == 0 else 1,
            detail_page_views=None, label_ids=["data.partial_metric_coverage"],
            behavior_label_scope="news_window_aggregate"))
    profiles = []
    for tenant in (TENANT, OTHER):
        for stamp in (START, END):
            for number, weights in enumerate(({"tech": 1, "tech.nature": 1}, {"tech": .5, "finance": 1}, {})):
                profiles.append(dict(tenant_id=tenant, synthetic_user_id=f"synthetic-{tenant[:8]}-{number}",
                    as_of=stamp.isoformat(), cohort_id="deep_reader", topic_weights=weights,
                    behavior_label_ids=[], counts={"clicks": number},
                    notification_allowed=number == 0, frequency_capped=number == 1,
                    source_mode="synthetic", profile_version=VERSION, events_through=stamp.isoformat()))
    (tmp_path / "policy.json").write_text(canonical(policy.model_dump(mode="json")), encoding="utf-8")
    for name, values in (("content_labels.jsonl", content), ("news_behavior_labels.jsonl", observations),
                         ("synthetic_profiles.jsonl", profiles), ("synthetic_events.jsonl", [])):
        (tmp_path / name).write_text("".join(canonical(value) + "\n" for value in values), encoding="utf-8")
    with sqlite3.connect(tmp_path / "label_index.sqlite") as db:
        db.executescript("""
            CREATE TABLE content_labels(tenant_id TEXT,news_id TEXT,payload_json TEXT);
            CREATE TABLE behavior_labels(tenant_id TEXT,news_id TEXT,window_start TEXT,window_end TEXT,
                source_dataset_version TEXT,scenario_id TEXT,metric_definition_id TEXT,payload_json TEXT);
            CREATE TABLE profiles(tenant_id TEXT,synthetic_user_id TEXT,as_of TEXT,payload_json TEXT);
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
        """)
        db.executemany("INSERT INTO content_labels VALUES(?,?,?)", [(row["tenant_id"], row["news_id"], canonical(row)) for row in content])
        db.executemany("INSERT INTO behavior_labels VALUES(?,?,?,?,?,?,?,?)", [(
            row["tenant_id"], row["news_id"], row["window_start"], row["window_end"], row["source_dataset_version"],
            row["scenario_id"], row["metric_definition_id"], canonical(row)) for row in observations])
        db.executemany("INSERT INTO profiles VALUES(?,?,?,?)", [(row["tenant_id"], row["synthetic_user_id"],
            row["as_of"], canonical(row)) for row in profiles])
        db.executemany("INSERT INTO metadata VALUES(?,?)", [("profile_version", VERSION),
            ("taxonomy_version", policy.catalog.taxonomy_version), ("policy_version", policy.policy_version)])
    manifest = dict(schema_version=1, package_version="behavior-tags-local-v1", profile_version=VERSION,
        taxonomy_version=policy.catalog.taxonomy_version, policy_version=policy.policy_version,
        source_window={"window_start": START.isoformat(), "window_end": END.isoformat()},
        build_as_of=END.isoformat(), profile_snapshot_times=[START.isoformat(), END.isoformat()],
        counts={"synthetic_profiles": len(profiles)}, artifacts={name: {"sha256": sha256((tmp_path / name).read_bytes()).hexdigest()}
            for name in ("policy.json", "label_index.sqlite", "content_labels.jsonl", "news_behavior_labels.jsonl",
                         "synthetic_profiles.jsonl", "synthetic_events.jsonl")})
    manifest["identity_sha256"] = sha256(canonical(manifest).encode()).hexdigest()
    (tmp_path / "manifest.json").write_text(canonical(manifest), encoding="utf-8")
    return LocalBehaviorRuntime(tmp_path, environment="test")


def app_for(index=None, token="TEST_GATEWAY_TOKEN"):
    app = FastAPI()
    app.include_router(router)
    app.state.behavior_tags_runtime = index
    app.dependency_overrides[get_data_loop_gateway_token] = lambda: token
    return app


def headers(tenant=TENANT, roles="hot-news:read"):
    return {"Authorization": "Bearer TEST_GATEWAY_TOKEN", "X-Tenant-ID": tenant,
            "X-User-ID": USER, "X-Hot-News-Roles": roles}


def body(**overrides):
    return dict(window_start=START.isoformat(), window_end=(START + timedelta(hours=1)).isoformat(), **overrides)


def test_search_tenant_comes_only_from_gateway_and_outputs_no_profiles(index):
    with TestClient(app_for(index)) as client:
        response = client.post("/api/v1/behavior-tags/search", headers=headers(), json=body())
        assert response.status_code == 200
        value = response.json()
        assert value["dispatch_enabled"] is False and value["source_mode"] == "synthetic"
        assert len(value["results"]) == 2
        assert all(row["behavior"]["tenant_id"] == TENANT for row in value["results"])
        assert {row["behavior"]["clicks"] for row in value["results"]} == {0, 2}
        assert all(row["behavior"]["detail_page_views"] is None for row in value["results"])
        assert not any(word in response.text for word in ("synthetic_user_id", "topic_weights", USER, "其他租户专属"))
        assert response.headers["Cache-Control"] == "no-store"
        other = client.post("/api/v1/behavior-tags/search", headers=headers(OTHER), json=body())
        assert [row["behavior"]["clicks"] for row in other.json()["results"]] == [999]


@pytest.mark.parametrize("extra", [{"tenant_id": OTHER}, {"user_id": USER}, {"synthetic_user_id": "synthetic-other"}])
def test_identity_and_individual_profile_fields_cannot_be_supplied_in_body(index, extra):
    with TestClient(app_for(index)) as client:
        assert client.post("/api/v1/behavior-tags/search", headers=headers(), json={**body(), **extra}).status_code == 422


def test_disabled_service_fails_closed_without_real_app_lifespan():
    with TestClient(app_for()) as client:
        assert client.get("/api/v1/behavior-tags/topics", headers=headers()).status_code == 503
        assert client.post("/api/v1/behavior-tags/search", headers=headers(), json=body()).status_code == 503


@pytest.mark.parametrize("kind,status", [("missing", 401), ("wrong_token", 401), ("no_read", 403), ("unconfigured", 503)])
def test_gateway_authentication_and_read_permission_are_required(index, kind, status):
    token = None if kind == "unconfigured" else "TEST_GATEWAY_TOKEN"
    request_headers = headers(roles="hot-news:decide" if kind == "no_read" else "hot-news:read")
    if kind == "missing":
        request_headers.pop("X-User-ID")
    if kind == "wrong_token":
        request_headers["Authorization"] = "Bearer WRONG"
    with TestClient(app_for(index, token)) as client:
        assert client.get("/api/v1/behavior-tags/topics", headers=request_headers).status_code == status


def test_topics_returns_bound_catalog_and_source_filters_keep_metric_worlds_separate(index):
    with TestClient(app_for(index)) as client:
        response = client.get("/api/v1/behavior-tags/topics", headers=headers())
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        assert response.json()["catalog"]["taxonomy_version"] == index.policy.catalog.taxonomy_version
        for filters, expected in (({"dataset_version": "dataset-v8"}, [0]),
                                  ({"scenario_id": "scene-v6"}, [2]),
                                  ({"dataset_version": "dataset-v8", "scenario_id": "scene-v6"}, [])):
            value = client.post("/api/v1/behavior-tags/search", headers=headers(), json=body(**filters)).json()
            assert [row["behavior"]["clicks"] for row in value["results"]] == expected


@pytest.mark.parametrize("change", [
    {"window_start": START.replace(tzinfo=None).isoformat()},
    {"window_end": START.isoformat()},
    {"window_end": (START + timedelta(days=61)).isoformat()},
    {"window_start": (START - timedelta(hours=1)).isoformat()},
    {"window_end": (END + timedelta(hours=1)).isoformat()},
    {"include_topic_ids": ["unknown-topic"]},
    {"include_topic_ids": ["tech"], "exclude_topic_ids": ["tech"]},
    {"limit": 101},
])
def test_invalid_time_label_or_limit_boundaries_fail_before_results(index, change):
    with TestClient(app_for(index)) as client:
        response = client.post("/api/v1/behavior-tags/search", headers=headers(), json={**body(), **change})
        assert response.status_code == 422


def test_search_uses_closed_window_containment_and_preserves_real_zero(index):
    with TestClient(app_for(index)) as client:
        first = client.post("/api/v1/behavior-tags/search", headers=headers(), json=body(dataset_version="dataset-v8"))
        assert first.json()["results"][0]["behavior"]["clicks"] == 0
        second = client.post("/api/v1/behavior-tags/search", headers=headers(), json={**body(),
            "window_start": (START + timedelta(hours=1)).isoformat(), "window_end": END.isoformat()})
        assert [row["behavior"]["clicks"] for row in second.json()["results"]] == [7]


@pytest.mark.asyncio
async def test_profile_aggregation_counts_each_profile_once_and_audience_watermark_is_exact(index):
    context = index.context_for_window(tenant_id=TENANT, window_start=START, window_end=END)
    assert context.profile_count == 3
    assert dict(context.topic_weights)["tech"] == .5  # Includes zero-interest cold-start profile.
    port = BehaviorPackageAudienceClient(index)
    query = AudienceQuery(tenant_id=UUID(TENANT), run_id=uuid4(), news_id="same-news", window_start=START,
        window_end=END, selection=AudienceSelection(taxonomy_version=index.policy.catalog.taxonomy_version,
                                                  include_topic_ids=("tech",), exclude_topic_ids=("finance",)))
    result = await port.estimate(query)
    assert (result.matched_users, result.eligible_users) == (1, 1)
    assert result.watermark == END and "synthetic_user_id" not in result.model_dump_json()
    with pytest.raises(AudienceProviderError):
        await port.estimate(query.model_copy(update={"window_end": START + timedelta(hours=1)}))
