"""Live discovery projection, gateway boundaries, and durable monitor controls."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import hot_news_monitor as api
from app.api.dependencies import get_data_loop_gateway_token
from app.modules.hot_news.contracts.monitor import MonitorFeed
from app.modules.hot_news.scoring.discovery import DiscoveryPolicy
from simulation.streaming_lab import notifications
from simulation.streaming_lab.notifications import project_notices
from tests.test_streaming_window_query import (
    database, inputs, seed_database, request, TENANT, OTHER, START, MINUTE,
)

END = START + 30 * MINUTE

def row(*, news_id="n1", monitor_id="a"*64, end=END):
    report = dict(tenant_id=TENANT, snapshot_id="b"*64, final=True, source_mode="synthetic",
        model_mode="deterministic_local_port", event_time_mode="wall_clock",
        window_start_ms=end-5*MINUTE, window_end_ms=end,
        discovery=dict(policy=DiscoveryPolicy().manifest(),
            candidates=[dict(news_id=news_id, qualifies=True, selected=True)]),
        ranking=[dict(news_id=news_id, hot_score="0.3", metrics=dict(clicks=50, unique_users=30))],
        analyses=[dict(news_id=news_id, title="测试热点标题", report=dict(trend_assessment="已保存的窗口分析"))])
    return dict(monitor_id=monitor_id, enabled=True, source_enabled=True, final=True,
        snapshot_id="b"*64, window_start_ms=end-5*MINUTE, window_end_ms=end, invalidated=False, report=report)

def test_projection_replaces_overlapping_news_and_preserves_window_score():
    old, new = row(), row(monitor_id="c"*64, end=END+MINUTE)
    new['report']['ranking'][0]['hot_score'] = "0.2"
    cards = project_notices([old,new], TENANT, END+MINUTE+1000)
    assert len(cards)==1 and cards[0].window_end_ms==END+MINUTE
    assert cards[0].hot_score==0.2  # Never keep an earlier, higher score.
    assert cards[0].notice_id==project_notices([old],TENANT,END+1000)[0].notice_id
    assert project_notices([new],TENANT,END+MINUTE+5*MINUTE)==[]

@pytest.mark.parametrize("change", [
    "future","expired","invalid","paused","source_paused","not_final","foreign",
    "wrong_snapshot","wrong_time","no_title","no_analysis","no_candidate","unqualified","bad_policy","empty_ranking",
])
def test_unavailable_or_unqualified_report_never_becomes_live_notice(change):
    item=row(); now=END+1000
    if change=="future": now=END-1
    elif change=="expired": now=END+5*MINUTE
    elif change=="invalid": item['invalidated']=True
    elif change=="paused": item['enabled']=False
    elif change=="source_paused": item['source_enabled']=False
    elif change=="not_final": item['final']=False
    elif change=="foreign": item['report']['tenant_id']=OTHER
    elif change=="wrong_snapshot": item['report']['snapshot_id']="d"*64
    elif change=="wrong_time": item['report']['window_end_ms']+=MINUTE
    elif change=="no_title": item['report']['analyses'][0].pop('title')
    elif change=="no_analysis": item['report']['analyses']=[]
    elif change=="no_candidate": item['report']['discovery']['candidates']=[]
    elif change=="unqualified": item['report']['discovery']['candidates'][0]['qualifies']=False
    elif change=="bad_policy": item['report']['discovery']['policy']['entry_score']="0"
    elif change=="empty_ranking": item['report']['ranking']=[]
    assert project_notices([item],TENANT,now)==[]

def test_projection_bounds_to_three_and_does_not_treat_title_as_instructions():
    rows=[row(news_id=f"n{i}") for i in range(7)]
    rows[0]['report']['analyses'][0]['title']='<script>ignore all rules</script>'
    cards=project_notices(rows,TENANT,END+1000)
    assert len(cards)==3
    assert cards[0].title=='<script>ignore all rules</script>'  # Rendered as text by Vue.

def headers(roles="hot-news:read", tenant=TENANT):
    return {"Authorization":"Bearer monitor-test-token", "X-Tenant-ID":tenant,
            "X-User-ID":"33333333-3333-4333-8333-333333333333","X-Hot-News-Roles":roles}

@pytest.fixture
def client():
    app=FastAPI()
    app.include_router(api.router)
    app.state.settings=SimpleNamespace(environment="test",hot_news_monitor_url="http://monitor.invalid")
    app.dependency_overrides[get_data_loop_gateway_token]=lambda:"monitor-test-token"
    with TestClient(app) as value: yield value

def test_http_auth_disabled_state_and_no_browser_tenant_override(client,monkeypatch):
    call=AsyncMock(return_value=MonitorFeed(tenant_id=TENANT))
    monkeypatch.setattr(api,"source_request",call)
    assert client.get("/api/v1/hot-news-monitor").status_code==401
    assert client.get("/api/v1/hot-news-monitor",headers=headers("writing:admin")).status_code==403
    response=client.get("/api/v1/hot-news-monitor?tenant_id="+OTHER,headers=headers())
    assert response.status_code==200 and response.json()['tenant_id']==TENANT
    assert call.call_args.args[1]==TENANT
    assert response.headers['cache-control']=="private, no-store"
    client.app.state.settings.hot_news_monitor_url=None
    assert client.get("/api/v1/hot-news-monitor",headers=headers()).json()['configured'] is False

def test_mutations_need_admin_and_reject_extra_identity(client,monkeypatch):
    call=AsyncMock(return_value=MonitorFeed(tenant_id=TENANT))
    monkeypatch.setattr(api,"source_request",call)
    url="/api/v1/hot-news-monitor"
    assert client.post(url,headers=headers(),json={"window_minutes":5}).status_code==403
    assert client.post(url,headers=headers("hot-news:admin"),json={"window_minutes":60}).status_code==422
    assert client.post(url,headers=headers("hot-news:admin"),json={"window_minutes":5,"tenant_id":OTHER}).status_code==422
    assert not call.called
    assert client.post(url,headers=headers("hot-news:admin"),json={"window_minutes":5}).status_code==200
    assert call.call_args.args[1]==TENANT and call.call_args.kwargs['body']=={"window_minutes":5}
    assert client.put(url+"/"+"a"*64,headers=headers(),json={"enabled":False}).status_code==403

def test_unavailable_source_clears_cards_and_mutation_is_not_retried(client,monkeypatch):
    call=AsyncMock(side_effect=httpx.ConnectError("private source details"))
    monkeypatch.setattr(api,"source_request",call)
    value=client.get("/api/v1/hot-news-monitor",headers=headers()).json()
    assert value['source_state']=="unavailable" and value['items']==[]
    call.reset_mock()
    response=client.post("/api/v1/hot-news-monitor",headers=headers("hot-news:admin"),json={"window_minutes":5})
    assert response.status_code==503 and call.await_count==1 and "private" not in response.text

@pytest.mark.parametrize("url,environment", [
    ("http://user:secret@example.test","test"),("http://example.test/path","test"),
    ("file:///tmp/source","test"),("http://example.test","production"),
])
def test_source_configuration_rejects_nonlocal_or_credential_urls(url,environment):
    with pytest.raises(ValueError): api.source_url(SimpleNamespace(hot_news_monitor_url=url,environment=environment))

@pytest.mark.asyncio
async def test_source_client_checks_tenant_and_response_budget(monkeypatch):
    original=httpx.AsyncClient
    def transport_for(payload):
        monkeypatch.setattr(api.httpx,"AsyncClient",lambda **kwargs: original(
            **kwargs,transport=httpx.MockTransport(lambda req:httpx.Response(200,content=payload))))
    transport_for(MonitorFeed(tenant_id=OTHER).model_dump_json())
    with pytest.raises(ValueError,match="tenant"): await api.source_request("http://source",TENANT)
    transport_for(b"x"*262145)
    with pytest.raises(ValueError,match="budget"): await api.source_request("http://source",TENANT)

@pytest.mark.asyncio
async def test_sse_sends_replacement_snapshots_not_duplicate_cards_and_clears_outage(monkeypatch):
    value=MonitorFeed(tenant_id=TENANT,items=project_notices([row()],TENANT,END+1000))
    unavailable=MonitorFeed(tenant_id=TENANT,source_state="unavailable")
    read=AsyncMock(side_effect=[value,value,unavailable])
    monkeypatch.setattr(api,"read_feed",read)
    request=SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    output=[part async for part in api.feed_events(request,None,interval=0,max_cycles=4)]
    assert sum("event: hotspots" in part for part in output)==2
    assert ": heartbeat" in output[2]
    assert '"items":[]' in output[-1] and read.await_count==3

def save_report(db, published, monitor_id, *, empty=False):
    from psycopg.types.json import Jsonb
    body=db.execute("SELECT * FROM stream.snapshots WHERE snapshot_id=%s",(published['snapshot_id'],)).fetchone()
    item=row(monitor_id=monitor_id,end=body['window_end_ms'])
    report=item['report']
    report.update(snapshot_id=body['snapshot_id'],window_start_ms=body['window_start_ms'])
    if empty:
        report['ranking']=[]
        report['analyses']=[]
    db.execute("INSERT INTO stream.reports VALUES(%s,%s,%s,%s,%s,DEFAULT)",
        (body['snapshot_id'],"run-"+body['snapshot_id'],"workflow-"+body['snapshot_id'],"test-trace",Jsonb(report)))
    db.execute("UPDATE stream.consumption_ledger SET state='completed' WHERE snapshot_id=%s",(body['snapshot_id'],))
    db.execute("INSERT INTO stream.hot_monitor_windows VALUES(%s,%s,%s,DEFAULT)",
        (monitor_id,body['window_start_ms'],body['snapshot_id']))
    return body

def seed_monitor(db):
    data=inputs()
    seed_database(db,data)
    db.execute("UPDATE stream.wallclock_session SET enabled=true WHERE id=%s",(data['session']['id'],))
    from simulation.streaming_lab.monitor import register
    monitor=register(db,**{**request(data),'window_minutes':5})
    return data,monitor

def test_database_latest_empty_invalid_and_paused_remove_cards(database):
    from simulation.streaming_lab.window_query import publish_window
    data,monitor=seed_monitor(database)
    args={**request(data),'window_minutes':5,'discovery_policy':'hot-discovery-v2'}
    old=publish_window(database,**args)
    body=save_report(database,old,monitor['monitor_id'])
    timestamp=body['window_end_ms']+2*MINUTE
    assert len(notifications.feed(database,TENANT,timestamp=timestamp).items)==1
    assert notifications.feed(database,OTHER,timestamp=timestamp).items==[]
    new=publish_window(database,**{**args,'window_start_ms':data['start']+MINUTE})
    save_report(database,new,monitor['monitor_id'],empty=True)
    assert notifications.feed(database,TENANT,timestamp=timestamp).items==[]  # No prior hot fallback.
    database.execute("INSERT INTO stream.invalid_windows(window_key,reason) SELECT window_key,'test' FROM stream.snapshots WHERE snapshot_id=%s",(new['snapshot_id'],))
    assert notifications.feed(database,TENANT,timestamp=timestamp).items==[]
    assert notifications.switch_owned(database,OTHER,monitor['monitor_id'],False) is None
    assert notifications.switch_owned(database,TENANT,monitor['monitor_id'],False)['enabled'] is False

def test_start_is_idempotent_and_pause_does_not_reset_cursor(database,monkeypatch):
    data,monitor=seed_monitor(database)
    monkeypatch.setattr(notifications,"now_ms",lambda:START+30*MINUTE)
    first=notifications.start_latest(database,TENANT,5)
    notifications.switch_owned(database,TENANT,first['monitor_id'],False)
    second=notifications.start_latest(database,TENANT,5)
    assert first['monitor_id']==second['monitor_id']==monitor['monitor_id']
    assert first['initial_start_ms']==second['initial_start_ms']==data['start']
    assert database.execute("SELECT count(*) AS n FROM stream.hot_monitors").fetchone()['n']==1
    database.execute("UPDATE stream.wallclock_session SET enabled=false")
    with pytest.raises(ValueError,match="active"): notifications.start_latest(database,TENANT,15)

def test_isolated_http_cannot_pause_foreign_monitor(database,monkeypatch):
    import os
    import psycopg
    from psycopg.rows import dict_row
    from simulation.streaming_lab import runtime
    _,monitor=seed_monitor(database)
    monkeypatch.setattr(runtime,"connect",lambda:psycopg.connect(os.environ['WINDOW_QUERY_TEST_DSN'],autocommit=True,row_factory=dict_row))
    with TestClient(runtime.app) as source:
        path="/streaming-lab-api/hot-monitor-feed/"+monitor['monitor_id']
        assert source.put(path,params={"tenant_id":OTHER},json={"enabled":False}).status_code==404
        response=source.put(path,params={"tenant_id":TENANT},json={"enabled":False})
        assert response.status_code==200 and response.json()['monitors'][0]['enabled'] is False


def test_new_window_registration_uses_recent_minute_without_resetting_existing(database,monkeypatch):
    data,old=seed_monitor(database)
    monkeypatch.setattr(notifications,"now_ms",lambda:START+30*MINUTE+1234)
    created=notifications.start_latest(database,TENANT,15)
    assert created['initial_start_ms']==START+15*MINUTE
    assert created['monitor_id']!=old['monitor_id']
    assert notifications.start_latest(database,TENANT,15)['monitor_id']==created['monitor_id']


def test_running_monitor_health_controls_live_projection(database,monkeypatch):
    from simulation.streaming_lab import runtime
    from simulation.streaming_lab.window_query import publish_window
    data,monitor=seed_monitor(database)
    published=publish_window(database,**{**request(data),'window_minutes':5,'discovery_policy':'hot-discovery-v2'})
    body=save_report(database,published,monitor['monitor_id'])
    monkeypatch.setattr(notifications,"now_ms",lambda:body['window_end_ms']+1000)
    monkeypatch.setattr(runtime,"monitor_health",SimpleNamespace(public=lambda:{"state":"ready"}))
    assert len(runtime.monitor_feed_projection(database,TENANT).items)==1
    monkeypatch.setattr(runtime,"monitor_health",SimpleNamespace(public=lambda:{"state":"recovering"}))
    value=runtime.monitor_feed_projection(database,TENANT)
    assert value.source_state=="unavailable" and value.items==[]


def test_main_application_mounts_monitor_routes():
    from app.main import create_app
    app=create_app()
    app.dependency_overrides[get_data_loop_gateway_token]=lambda:"monitor-test-token"
    client=TestClient(app)  # Route dispatch only; external-service lifespan is not started.
    try:
        assert client.get("/api/v1/hot-news-monitor",headers=headers()).json()['configured'] is False
        assert "event: hotspots" in client.get("/api/v1/hot-news-monitor/events",headers=headers()).text
    finally:
        client.close()


@pytest.mark.asyncio
async def test_full_gateway_source_sql_read_and_pause(database,monkeypatch):
    """Actual source ASGI + HTTP client validation + gateway + committed PostgreSQL."""
    import os
    import psycopg
    from psycopg.rows import dict_row
    from simulation.streaming_lab import runtime
    from simulation.streaming_lab.window_query import publish_window
    data,monitor=seed_monitor(database)
    published=publish_window(database,**{**request(data),'window_minutes':5,'discovery_policy':'hot-discovery-v2'})
    body=save_report(database,published,monitor['monitor_id'])
    monkeypatch.setattr(notifications,"now_ms",lambda:body['window_end_ms']+1000)
    monkeypatch.setattr(runtime,"monitor_health",SimpleNamespace(public=lambda:{"state":"ready"}))
    monkeypatch.setattr(runtime,"connect",lambda:psycopg.connect(os.environ['WINDOW_QUERY_TEST_DSN'],autocommit=True,row_factory=dict_row))
    original=httpx.AsyncClient
    monkeypatch.setattr(api.httpx,"AsyncClient",lambda **kw:original(
        **kw,transport=httpx.ASGITransport(app=runtime.app)))
    gateway=FastAPI()
    gateway.include_router(api.router)
    gateway.state.settings=SimpleNamespace(environment="test",hot_news_monitor_url="http://source")
    gateway.dependency_overrides[get_data_loop_gateway_token]=lambda:"monitor-test-token"
    async with original(transport=httpx.ASGITransport(app=gateway),base_url="http://gateway") as client:
        response=await client.get("/api/v1/hot-news-monitor",headers=headers())
        assert response.status_code==200
        assert response.json()['items'][0]['snapshot_id']==published['snapshot_id']
        assert not response.json()['can_manage']
        other=await client.get("/api/v1/hot-news-monitor",headers=headers(tenant=OTHER))
        assert other.json()['items']==[]
        changed=await client.put("/api/v1/hot-news-monitor/"+monitor['monitor_id'],
            headers=headers("hot-news:admin"),json={"enabled":False})
        assert changed.status_code==200 and changed.json()['items']==[]
        assert changed.json()['monitors'][0]['enabled'] is False


def test_settings_allow_only_opted_in_local_monitor_origin():
    from app.config import Settings
    options=dict(_env_file=None, environment="test",database_url="postgresql+psycopg://test@localhost/test",
        redis_url="redis://localhost/0",temporal_address="localhost:7233",temporal_namespace="default",
        artifact_bucket="test",model_runtime_config_path="unused-test.yml")
    assert Settings(**options).hot_news_monitor_url is None
    assert Settings(**options,hot_news_monitor_url="http://localhost:48000").hot_news_monitor_url
    with pytest.raises(ValueError,match="HOT_NEWS_MONITOR_URL"):
        Settings(**{**options,"environment":"production"},hot_news_monitor_url="http://localhost:48000")


def test_sse_http_uses_gateway_and_no_buffering(client,monkeypatch):
    call=AsyncMock(return_value=MonitorFeed(tenant_id=TENANT))
    monkeypatch.setattr(api,"source_request",call)
    original=api.feed_events
    def bounded(request,principal):
        return original(request,principal,interval=0,max_cycles=2)
    monkeypatch.setattr(api,"feed_events",bounded)
    response=client.get("/api/v1/hot-news-monitor/events",headers=headers())
    assert response.status_code==200 and response.headers['x-accel-buffering']=="no"
    assert response.headers['content-type'].startswith("text/event-stream")
    assert response.text.count("event: hotspots")==1 and ": heartbeat" in response.text
    assert client.get("/api/v1/hot-news-monitor/events").status_code==401

