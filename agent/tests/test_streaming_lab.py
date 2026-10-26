"""Contract/identity tests for the isolated streaming adapter, independent of infrastructure."""
from hashlib import sha256
import json
import pytest
from simulation.streaming_lab.bridge import (
    validate_payload, SnapshotRunRequest, FrozenSnapshotMetricSource,
    analyze_snapshot, utc, BUNDLE,
)
from app.modules.hot_news.scoring.metric_source import HotNewsMetricQuery

TENANT='11111111-1111-4111-8111-111111111111'


def fixture_payload(**updates):
    data={'tenant_id':TENANT,'run_id':'contract-test','window_start_ms':1800000000000,
        'window_end_ms':1800000060000,'metric_version':'synthetic-event-metrics-v1',
        'event_count':20,'event_digest':'a'*64,'final':False,
        'metrics':[{'news_id':'synthetic-article-1','content_type':'article','impressions':10,
            'clicks':4,'unique_users':3,'total_duration_seconds':60,'effective_consumptions':2,'interactions':1}]}
    data.update(updates)
    text=json.dumps(data)
    return text,sha256(text.encode()).hexdigest()


def test_hash_tenant_and_version_fail_closed():
    text,sid=fixture_payload()
    assert validate_payload(text,sid,TENANT)['event_count']==20
    for invalid in [(text+' ',sid,TENANT),(text,sid,'22222222-2222-4222-8222-222222222222')]:
        with pytest.raises(ValueError): validate_payload(*invalid)
    text,sid=fixture_payload(metric_version='unapproved')
    with pytest.raises(ValueError): validate_payload(text,sid,TENANT)


def test_revision_changes_run_identity_without_changing_bundle():
    args=dict(tenant_id=TENANT,window_start=utc(1800000000000),window_end=utc(1800000060000),production_bundle_version=BUNDLE)
    a=SnapshotRunRequest(**args,snapshot_sha256='a'*64)
    b=SnapshotRunRequest(**args,snapshot_sha256='b'*64)
    assert a.idempotency_key != b.idempotency_key
    assert a.idempotency_key == SnapshotRunRequest(**args,snapshot_sha256='a'*64).idempotency_key
    with pytest.raises(ValueError): SnapshotRunRequest(**args).validate()


@pytest.mark.asyncio
async def test_source_rejects_cross_window_query():
    text,sid=fixture_payload()
    source=FrozenSnapshotMetricSource(text,sid,TENANT)
    with pytest.raises(ValueError):
        await source.fetch_snapshots(HotNewsMetricQuery(tenant_id=TENANT,window_start=utc(1800000060000),window_end=utc(1800000120000)))


@pytest.mark.asyncio
async def test_existing_hot_news_domain_chain_uses_only_frozen_metrics():
    text,sid=fixture_payload()
    result=await analyze_snapshot(text,sid,TENANT)
    assert result['external_model_calls']==0
    assert result['ranking'][0]['metrics']['clicks']==4
    assert result['analyses'][0]['model_request_id'].startswith('native-local-')
    assert len(result['analyses'])==1
    assert result['analyses'][0]['report']['limitations']


@pytest.mark.parametrize('changes',[{'window_end_ms':1800000120000},{'event_count':10001},{'final':'true'}])
def test_budget_and_time_contract(changes):
    text,sid=fixture_payload(**changes)
    with pytest.raises(ValueError): validate_payload(text,sid,TENANT)
