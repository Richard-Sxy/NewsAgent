"""Integrity, temporal isolation and actual interest-score behavior, without network."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.analytics.entities import ContentType
from app.analytics.news_content import NewsContent
from app.modules.audience.behavior_package import BehaviorPackageAudienceClient
from app.modules.audience.contracts import AudienceProviderError, AudienceQuery, AudienceSelection
from app.modules.behavior_tags.runtime import LocalBehaviorRuntime, build_behavior_runtime
from app.modules.behavior_tags.tagging import load_topic_policy
from app.clients.knowledge_base import RelatedNews
from app.modules.hot_news.analysis.related_news_reranker import RerankRelatedNews

TENANT = "11111111-1111-4111-8111-111111111111"
START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = datetime(2026, 10, 3, tzinfo=timezone.utc)
POLICY = Path(__file__).resolve().parents[1] / "deploy/behavior-topic-policy.local.yml"


@pytest.fixture
def package(tmp_path):
    policy = load_topic_policy(POLICY)
    (tmp_path / "policy.json").write_text(policy.model_dump_json(), encoding="utf-8")
    with sqlite3.connect(tmp_path / "label_index.sqlite") as db:
        db.execute("CREATE TABLE profiles(tenant_id TEXT,synthetic_user_id TEXT,as_of TEXT,payload_json TEXT)")
        db.execute("CREATE TABLE content_labels(tenant_id TEXT,news_id TEXT,payload_json TEXT)")
        db.execute("CREATE TABLE behavior_labels(tenant_id TEXT,news_id TEXT,window_start TEXT,window_end TEXT,payload_json TEXT)")
        for as_of in [START, END]:
            for number, (topics, allowed, capped) in enumerate([
                ({"tech": 1, "tech.nature": 1}, True, False),
                ({"tech": .8, "finance": .8}, False, False),
                ({"tech": .6}, True, True),
            ]):
                profile = dict(tenant_id=TENANT, synthetic_user_id=f"synthetic-{number}",
                    as_of=as_of.isoformat(), cohort_id="deep_reader", topic_weights=topics,
                    notification_allowed=allowed, frequency_capped=capped, profile_version="test-profile-v1")
                db.execute("INSERT INTO profiles VALUES(?,?,?,?)", (TENANT, profile["synthetic_user_id"],
                    as_of.isoformat(), json.dumps(profile)))
    manifest = dict(schema_version=1, taxonomy_version=policy.catalog.taxonomy_version,
        policy_version=policy.policy_version, profile_version="test-profile-v1",
        source_window={"window_start": START.isoformat(), "window_end": END.isoformat()},
        artifacts={name: {"sha256": hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()}
                   for name in ["policy.json", "label_index.sqlite"]})
    manifest["identity_sha256"] = hashlib.sha256(json.dumps(manifest, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def runtime(package, **kwargs):
    return LocalBehaviorRuntime(package, environment="test", **kwargs)


def test_integrity_and_production_guard(package):
    with pytest.raises(ValueError, match="restricted"):
        LocalBehaviorRuntime(package, environment="production")
    (package / "policy.json").write_text("{}")
    with pytest.raises(ValueError, match="digest mismatch"):
        runtime(package)


def test_manifest_version_cannot_be_changed_without_identity(package):
    path = package / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["profile_version"] = "forged-v2"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest identity"):
        runtime(package)


def test_time_tenant_and_context_identity(package):
    item = runtime(package)
    early = item.context_for_window(tenant_id=TENANT, window_start=START,
        window_end=datetime(2026, 10, 2, tzinfo=timezone.utc))
    assert early.profile_as_of == START.isoformat()
    late = item.context_for_window(tenant_id=TENANT, window_start=START, window_end=END)
    assert early.identity_sha256 != late.identity_sha256
    assert late.identity_sha256 != runtime(package, max_bonus=.02).context_for_window(
        tenant_id=TENANT, window_start=START, window_end=END).identity_sha256
    with pytest.raises(ValueError, match="no non-future"):
        item.context_for_window(tenant_id=str(uuid4()), window_start=START, window_end=END)
    with pytest.raises(ValueError, match="does not cover"):
        item.context_for_window(tenant_id=TENANT, window_start=START, window_end=END.replace(day=4))


def candidate(news_id, title, score, reasons=()):
    return RerankRelatedNews(RelatedNews(news_id=news_id, title=title, text=title,
        collection_id="test", source_url="https://example.com", publish_time=START.isoformat(), score=.9),
        score, .9, 0, 0, 0, 0, reasons)


def test_bonus_reorders_survivors_and_keeps_conflict_penalty(package):
    context = runtime(package).context_for_window(tenant_id=TENANT, window_start=START, window_end=END)
    source = NewsContent("source", "生物多样性与生态系统研究", "生态系统研究", ContentType.ARTICLE, START, "https://example.com")
    base = [candidate("a", "篮球体育比赛", .55), candidate("b", "生物多样性研究", .52),
            candidate("c", "生态系统研究", .53, ("关键主体不一致，执行降权",))]
    result, trace = context.rerank(source, base, limit=3)
    assert [item.news.news_id for item in result] == ["b", "a", "c"]
    assert base[1].final_score == .52  # input immutable
    assert trace["candidates"][2]["interest_bonus"] == 0
    assert all(0 <= row["interest_bonus"] <= .08 for row in trace["candidates"])
    assert not trace["hot_score_changed"]
    assert "synthetic_user_id" not in json.dumps(trace)
    assert set(trace["after_news_ids"]) <= {"a", "b", "c"}


@pytest.mark.asyncio
async def test_audience_exact_set_semantics_and_no_personal_output(package):
    port = BehaviorPackageAudienceClient(runtime(package))
    query = AudienceQuery(tenant_id=TENANT, run_id=uuid4(), news_id="a", window_start=START,
        window_end=END, selection=AudienceSelection(taxonomy_version=port.runtime.policy.catalog.taxonomy_version,
            include_topic_ids=("tech",), exclude_topic_ids=("finance",)))
    result = await port.estimate(query)
    assert (result.matched_users, result.eligible_users) == (2, 1)
    both = query.model_copy(update={"selection": query.selection.model_copy(update={
        "include_topic_ids": ("tech", "finance"), "exclude_topic_ids": (), "match": "all"})})
    assert (await port.estimate(both)).matched_users == 1
    assert "synthetic_user_id" not in result.model_dump_json()
    with pytest.raises(AudienceProviderError, match="not valid"):
        await port.estimate(query.model_copy(update={"window_end": END.replace(day=2)}))
    await port.close()
    with pytest.raises(AudienceProviderError, match="closed"):
        await port.estimate(query)


def test_factory_disabled_requires_no_files():
    assert build_behavior_runtime(SimpleNamespace(behavior_tags_enabled=False)) is None
