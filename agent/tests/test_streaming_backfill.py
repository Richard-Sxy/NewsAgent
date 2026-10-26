from hashlib import sha256
import json

import pytest
from simulation.streaming_lab.backfill import canonical, prepare, recount

TENANT = '11111111-1111-4111-8111-111111111111'
IDENTITY = dict(tenant_id=TENANT,run_id='backfill-contract',window_start_ms=1800000000000)


def event(name, kind, user='synthetic-user-1', **changes):
    return dict(schema_version=1,synthetic=True,kind='event',**IDENTITY,
                event_id=f"backfill-contract:1800000000000:{name}",event_time_ms=1800000001000,
                news_id='synthetic-article-1',content_type='article',event_type=kind,user_id=user,
                duration_seconds=0,**changes)


def fixture():
    events=[event('i','impression'),event('c','click')]
    body=recount(events,IDENTITY)
    text=canonical(body)
    base=dict(payload_text=text,snapshot_id=sha256(text.encode()).hexdigest())
    package=dict(tenant_id=TENANT,base_events=events,added_events=[event('li','impression'),event('lc','click')])
    return package,base


def test_full_recount_deduplicates_click_users_across_old_and_late_events():
    package,base=fixture()
    package['added_events'] += [package['base_events'][0],package['added_events'][1]]
    _,_,body,count=prepare(package,base)
    assert count == 2
    assert body['metrics'][0]['clicks'] == 2
    assert body['metrics'][0]['unique_users'] == 1


@pytest.mark.parametrize('field,value',[
    ('tenant_id','22222222-2222-4222-8222-222222222222'),
    ('event_time_ms',1800000060000),('synthetic',False),('duration_seconds',True),
    ('user_id','real-person'),('event_type','read-private'),('schema_version',True),
])
def test_event_contract_rejects_foreign_data(field,value):
    package,base=fixture()
    package['added_events'][0][field]=value
    with pytest.raises(ValueError): prepare(package,base)


def test_digest_detects_base_substitution_even_when_metrics_are_identical():
    package,base=fixture()
    package['base_events'][0]['user_id']='synthetic-user-99'
    with pytest.raises(ValueError,match='reproduce'): prepare(package,base)


def test_conflicting_identity_and_empty_addition_are_rejected():
    package,base=fixture()
    package['added_events']=[dict(package['base_events'][0],user_id='synthetic-user-2')]
    with pytest.raises(ValueError,match='Conflicting'): prepare(package,base)
    package['added_events']=[package['base_events'][0]]
    with pytest.raises(ValueError,match='no new'): prepare(package,base)


def test_nonfinal_base_is_rejected_and_semantic_order_does_not_affect_digest():
    package,base=fixture()
    assert recount(list(reversed(package['base_events'])),IDENTITY) == json.loads(base['payload_text'])
    body=json.loads(base['payload_text']);body['final']=False
    text=canonical(body);base.update(payload_text=text,snapshot_id=sha256(text.encode()).hexdigest())
    with pytest.raises(ValueError,match='finalized'): prepare(package,base)
