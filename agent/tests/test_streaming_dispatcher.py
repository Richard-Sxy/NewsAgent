import asyncio

import psycopg
import pytest
from simulation.streaming_lab.dispatcher import action_for, dispatch_once, run_dispatcher, DispatcherHealth
from simulation.streaming_lab.dispatcher import TemporalTransport, SqlLedger


class Ledger:
    def __init__(self):
        self.row=dict(snapshot_id='a'*64,workflow_id='workflow',lease_token='lease',dispatch_attempts=0,has_report=False,invalidated=False)
        self.state='pending';self.events=[];self.fail_ack=False
    def claim(self): return dict(self.row) if self.state in ('pending','started') else None
    def reserve_start(self,row):
        assert self.row['dispatch_attempts']<2
        self.row['dispatch_attempts']+=1;self.events.append('reserved');return True
    def finish(self,row,state,error=None,delay=3):
        if self.fail_ack: self.fail_ack=False;raise psycopg.OperationalError('ack disconnected')
        self.state=state or self.state;self.events.append((self.state,error))


class Transport:
    def __init__(self): self.status=None;self.calls=0;self.outage=False;self.lost_ack=False
    async def check(self):
        if self.outage: raise ConnectionError('offline')
    async def describe(self,workflow_id): return self.status
    async def start(self,row):
        self.calls+=1;self.status='RUNNING'
        if self.lost_ack: self.lost_ack=False;raise TimeoutError('response lost')


@pytest.mark.asyncio
async def test_uncertain_start_describes_same_workflow_before_any_retry():
    ledger,transport=Ledger(),Transport();transport.lost_ack=True
    with pytest.raises(TimeoutError): await dispatch_once(ledger,transport)
    assert ledger.row['dispatch_attempts']==1
    await dispatch_once(ledger,transport)
    assert transport.calls==1 and ledger.state=='started'
    ledger.row['has_report']=True;transport.status='COMPLETED'
    await dispatch_once(ledger,transport)
    assert ledger.state=='completed'


@pytest.mark.asyncio
async def test_db_ack_failure_after_start_reconciles_without_second_submission():
    ledger,transport=Ledger(),Transport();ledger.fail_ack=True
    with pytest.raises(psycopg.OperationalError): await dispatch_once(ledger,transport)
    await dispatch_once(ledger,transport)
    assert transport.calls==1 and ledger.state=='started'


@pytest.mark.asyncio
async def test_dependency_outage_does_not_spend_submission_budget():
    ledger,transport=Ledger(),Transport();transport.outage=True
    with pytest.raises(ConnectionError): await dispatch_once(ledger,transport)
    assert ledger.row['dispatch_attempts']==0 and transport.calls==0


@pytest.mark.parametrize('status,attempts,report,invalid,expected',[
    (None,0,False,False,('start',None)),
    (None,2,False,False,('failed','StartAttemptsExhausted')),
    ('COMPLETED',1,False,False,('failed','MissingDurableReport')),
    ('COMPLETED',1,True,False,('completed',None)),
    ('FAILED',1,False,False,('failed','FAILED')),
    ('RUNNING',1,False,False,('started',None)),
    ('COMPLETED',1,True,True,('failed','InvalidatedWindow')),
])
def test_state_machine(status,attempts,report,invalid,expected):
    assert action_for(status=status,attempts=attempts,has_report=report,invalidated=invalid)==expected


@pytest.mark.asyncio
async def test_runtime_recovery_backoff_is_bounded_and_cancellation_is_not_swallowed():
    ledger,transport=Ledger(),Transport();transport.outage=True
    health=DispatcherHealth();delays=[]
    async def sleep(delay):
        delays.append(delay)
        if len(delays)==6: transport.outage=False
        if len(delays)==7: raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): await run_dispatcher(ledger,transport,health,sleep=sleep)
    assert delays[:6]==[1,2,4,8,15,15]
    assert health.public()['state']=='ready' and health.consecutive_failures==0
    assert transport.calls==1


@pytest.mark.asyncio
async def test_programming_errors_fail_loudly_instead_of_forever_retrying():
    ledger,transport=Ledger(),Transport()
    def broken(): raise ValueError('broken schema')
    ledger.claim=broken
    with pytest.raises(ValueError): await run_dispatcher(ledger,transport,DispatcherHealth())


@pytest.mark.asyncio
async def test_application_deadline_bounds_sdk_reconnect_wait(monkeypatch):
    from types import SimpleNamespace
    original=asyncio.wait_for
    observed=[]
    async def bounded(awaitable,*,timeout):
        observed.append(timeout)
        return await original(awaitable,timeout=.01)
    async def hanging(**kwargs): await asyncio.sleep(3600)
    monkeypatch.setattr(asyncio,'wait_for',bounded)
    transport=TemporalTransport(SimpleNamespace(service_client=SimpleNamespace(check_health=hanging)),'q')
    with pytest.raises(TimeoutError): await transport.check()
    assert observed==[5]


@pytest.mark.asyncio
async def test_two_uncertain_submissions_without_remote_workflow_stop_at_budget():
    ledger,transport=Ledger(),Transport()
    async def never_started(row):
        transport.calls+=1
        raise TimeoutError('not delivered')
    transport.start=never_started
    for _ in range(2):
        with pytest.raises(TimeoutError): await dispatch_once(ledger,transport)
    await dispatch_once(ledger,transport)
    assert ledger.state=='failed' and transport.calls==2


@pytest.fixture
def sql_ledger():
    import os
    from contextlib import contextmanager
    from hashlib import sha256
    from uuid import uuid4
    import json
    if os.environ.get('LAB_DISPATCH_SQL_TEST')!='1': pytest.skip('Explicit isolated SQL test opt-in')
    from simulation.streaming_lab.runtime import connect
    with connect() as db, db.transaction(force_rollback=True):
        run_id='lease-test-'+uuid4().hex
        key='11111111-1111-4111-8111-111111111111/'+run_id+'/1800000000000'
        payload=json.dumps({'synthetic_sql_test':run_id});sid=sha256(payload.encode()).hexdigest()
        db.execute('INSERT INTO stream.window_heads(window_key) VALUES(%s)',(key,))
        db.execute('INSERT INTO stream.snapshots(snapshot_id,window_key,revision,tenant_id,run_id,window_start_ms,window_end_ms,final,payload_text) VALUES(%s,%s,1,%s,%s,1800000000000,1800000060000,true,%s)',(sid,key,key.split('/')[0],run_id,payload))
        db.execute('INSERT INTO stream.consumption_ledger(snapshot_id,workflow_id) VALUES(%s,%s)',(sid,'test-'+sid))
        @contextmanager
        def same_connection(): yield db
        yield SqlLedger(same_connection),db,sid
        # Outer rollback prevents any test fixture being visible to live consumers.


def test_sql_expired_lease_is_fenced_and_start_budget_is_atomic(sql_ledger):
    ledger,db,sid=sql_ledger
    old=ledger.claim();assert old['snapshot_id']==sid
    assert ledger.claim() is None
    db.execute("UPDATE stream.consumption_ledger SET lease_until=clock_timestamp()-interval '1 second' WHERE snapshot_id=%s",(sid,))
    new=ledger.claim();assert new['lease_token']!=old['lease_token']
    ledger.finish(old,'completed')
    assert not ledger.reserve_start(old)
    assert ledger.reserve_start(new) and ledger.reserve_start(new)
    assert not ledger.reserve_start(new)
    assert db.execute('SELECT state FROM stream.consumption_ledger WHERE snapshot_id=%s',(sid,)).fetchone()['state']=='pending'


def test_sql_delayed_ack_cannot_overwrite_activity_completion(sql_ledger):
    ledger,db,sid=sql_ledger
    row=ledger.claim();assert row['snapshot_id']==sid
    db.execute("UPDATE stream.consumption_ledger SET state='completed',error_type=NULL WHERE snapshot_id=%s",(sid,))
    ledger.finish(row,'started','TimeoutError')
    actual=db.execute('SELECT state,error_type,lease_token FROM stream.consumption_ledger WHERE snapshot_id=%s',(sid,)).fetchone()
    assert actual==dict(state='completed',error_type=None,lease_token=None)
