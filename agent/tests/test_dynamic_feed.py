"""Determinism, scenario evolution, exact UV and persistent replay safety."""
import os
import pytest
from temporalio.exceptions import ApplicationError
from simulation.streaming_lab import dynamic_feed as feed
from simulation.streaming_lab.dynamic_data import packet, digest, enforce_budget, TENANTS
from tools.streaming_lab_acceptance import recount

START = 1800000000000


def test_replay_reconstructs_identical_packet_and_hash():
    assert packet(7,START) == packet(7,START)
    assert digest(packet(7,START)) != digest(packet(8,START))


def test_original_version_remains_byte_reproducible():
    assert digest(packet(2,1792681380000,'dynamic-traffic-v1')) == 'eeede3d582444106159d6694f725b97bddeb716d9dae52d087bad9b5aab60dd6'


@pytest.mark.asyncio
async def test_hot_ranking_leader_actually_changes_with_scenario():
    from simulation.streaming_lab.bridge import analyze_snapshot
    from simulation.streaming_lab.dynamic_data import encode
    from hashlib import sha256
    leaders=[]
    for number in range(1,7):
        value=packet(number,START)
        metrics,count=recount(value['events'],TENANTS[0])
        body=dict(tenant_id=TENANTS[0],run_id=value['run_id'],window_start_ms=START,window_end_ms=START+60000,
                  metric_version='synthetic-event-metrics-v1',final=True,event_count=count,event_digest='0'*64,metrics=metrics)
        payload=encode(body).decode()
        result=await analyze_snapshot(payload,sha256(payload.encode()).hexdigest(),TENANTS[0])
        leaders.append(result['ranking'][0]['news_id'])
    assert len(set(leaders))==3


@pytest.mark.parametrize('sequence',range(1,7))
def test_all_scenarios_preserve_funnel_and_exact_uv(sequence):
    value = packet(sequence,START)
    assert len(value['events']) < 10000
    for tenant in TENANTS:
        rows,count = recount(value['events'],tenant)
        assert count < len(value['events'])
        for row in rows:
            assert 0 < row['unique_users'] < row['clicks'] <= row['impressions']
            assert row['effective_consumptions'] <= row['clicks']
        assert all(event['event_time_ms'] < START+60000 for event in value['events'])


def test_traffic_changes_news_leader_and_tenant_preferences():
    leaders=[]
    for number in range(1,7):
        by_tenant=[recount(packet(number,START)['events'],t)[0] for t in TENANTS]
        assert by_tenant[0] != by_tenant[1]
        leaders.append(max(by_tenant[0],key=lambda r:r['clicks'])['news_id'])
    assert len(set(leaders)) == 3


@pytest.mark.parametrize('change',[{'database_bytes':512*1024**2},{'backlog':41},{'lag':91},{'failures':1}])
def test_resource_or_consumer_problem_stops_generation(change):
    value=dict(database_bytes=1000,backlog=0,lag=0,failures=0)
    value.update(change)
    with pytest.raises(ValueError): enforce_budget(**value)


def test_another_producer_lock_is_preserved(tmp_path,monkeypatch):
    monkeypatch.setattr(feed,'STATE',tmp_path)
    (tmp_path/'producer.lock').write_text('another-run',encoding='utf-8')
    with pytest.raises(ValueError): feed.ensure_lock()
    feed.release_lock()
    assert (tmp_path/'producer.lock').read_text()=='another-run'


@pytest.mark.skipif(os.environ.get('LAB_DYNAMIC_SQL_TEST')!='1',reason='explicit isolated SQL only')
def test_sql_restart_reuses_batch_and_reservations_are_bounded(tmp_path,monkeypatch):
    monkeypatch.setattr(feed,'STATE',tmp_path)
    with feed.connect() as db, db.transaction(force_rollback=True):
        db.execute('UPDATE stream.dynamic_feed SET enabled=true WHERE id=1')
        first,_=feed.plan(db,'sql-test-activity')
        repeated,_=feed.plan(db,'sql-test-activity')
        assert first['run_id']==repeated['run_id'] and first['packet_sha256']==repeated['packet_sha256']
        feed.reserve(db,first['sequence'],'data')
        feed.reserve(db,first['sequence'],'data')
        with pytest.raises(ApplicationError): feed.reserve(db,first['sequence'],'data')
        db.execute("UPDATE stream.dynamic_batches SET state='data_sent' WHERE sequence=%s",(first['sequence'],))
        with pytest.raises(ApplicationError): feed.reserve(db,first['sequence'],'data')
        feed.reserve(db,first['sequence'],'marker')
        # Disable with an unfinished batch: keep producer lock for safe reconciliation.
        db.execute('UPDATE stream.dynamic_feed SET enabled=false WHERE id=1')
        assert feed.plan(db,'next-paused-activity')[0] is None
        assert (tmp_path/'producer.lock').exists()
        db.execute("UPDATE stream.dynamic_batches SET state='completed' WHERE sequence=%s",(first['sequence'],))
        # Simulate a lost completion acknowledgement after the SQL transaction committed.
        assert feed.plan(db,'sql-test-activity')[0]['sequence']==first['sequence']
        assert feed.plan(db,'next-paused-activity')[0] is None
        assert not (tmp_path/'producer.lock').exists()
