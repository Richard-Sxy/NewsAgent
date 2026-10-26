"""Leased dispatch, crash reconciliation and bounded logical submissions.

Database/Temporal connectivity failures delay work instead of consuming submission
attempts. An uncertain start is described before another start is ever attempted.
Only error classes and IDs enter the operational ledger, never payloads or DSNs.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import time
from uuid import uuid4

import psycopg
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode


TERMINAL = {'FAILED','TIMED_OUT','TERMINATED','CANCELED'}


def action_for(*, status, attempts, has_report, invalidated):
    if invalidated: return 'failed', 'InvalidatedWindow'
    if has_report: return 'completed', None
    if status == 'COMPLETED': return 'failed', 'MissingDurableReport'
    if status in TERMINAL: return 'failed', status
    if status is not None: return 'started', None
    if attempts >= 2: return 'failed', 'StartAttemptsExhausted'
    return 'start', None


class SqlLedger:
    def __init__(self, connect): self.connect = connect

    def claim(self):
        token = str(uuid4())
        with self.connect() as db, db.transaction():
            row = db.execute('''SELECT l.*, EXISTS(SELECT 1 FROM stream.reports r WHERE r.snapshot_id=l.snapshot_id) AS has_report,
                EXISTS(SELECT 1 FROM stream.invalid_snapshots i WHERE i.snapshot_id=l.snapshot_id) AS invalidated
                FROM stream.consumption_ledger l WHERE state IN ('pending','started') AND next_attempt_at<=clock_timestamp()
                AND (lease_until IS NULL OR lease_until<clock_timestamp()) ORDER BY next_attempt_at,snapshot_id
                LIMIT 1 FOR UPDATE OF l SKIP LOCKED''').fetchone()
            if not row: return None
            db.execute("UPDATE stream.consumption_ledger SET lease_token=%s,lease_until=clock_timestamp()+interval '30 seconds' WHERE snapshot_id=%s",(token,row['snapshot_id']))
            return {**row,'lease_token':token}

    def reserve_start(self, row):
        with self.connect() as db, db.transaction():
            result=db.execute('''UPDATE stream.consumption_ledger SET dispatch_attempts=dispatch_attempts+1,updated_at=clock_timestamp()
                WHERE snapshot_id=%s AND lease_token=%s AND lease_until>clock_timestamp()
                AND state IN ('pending','started') AND dispatch_attempts<2 RETURNING dispatch_attempts''',
                (row['snapshot_id'],row['lease_token'])).fetchone()
            if not result: return False
            self._event(db,row,'start_reserved',result['dispatch_attempts'],None)
            return True

    @staticmethod
    def _event(db,row,action,attempt,error):
        db.execute('INSERT INTO stream.dispatch_events(snapshot_id,action,attempt,error_type) VALUES(%s,%s,%s,%s)',
                   (row['snapshot_id'],action,attempt,error))

    def finish(self,row,state,error=None,delay=3):
        with self.connect() as db, db.transaction():
            old=db.execute('SELECT state,error_type,dispatch_attempts FROM stream.consumption_ledger WHERE snapshot_id=%s AND lease_token=%s FOR UPDATE',
                           (row['snapshot_id'],row['lease_token'])).fetchone()
            if not old: return  # Expired claim reclaimed elsewhere; never acknowledge another owner's attempt.
            if old['state'] in ('completed','failed'): state,error=old['state'],old['error_type']
            else: state=state or old['state']
            changed=old['state']!=state or old['error_type']!=error
            db.execute("UPDATE stream.consumption_ledger SET state=%s,error_type=%s,lease_token=NULL,lease_until=NULL,next_attempt_at=clock_timestamp()+(%s * interval '1 second'),updated_at=clock_timestamp() WHERE snapshot_id=%s AND lease_token=%s",
                       (state,error,delay,row['snapshot_id'],row['lease_token']))
            if changed: self._event(db,row,state,old['dispatch_attempts'],error)


class TemporalTransport:
    def __init__(self, client: Client, queue): self.client,self.queue=client,queue

    async def check(self):
        if not await asyncio.wait_for(self.client.service_client.check_health(timeout=timedelta(seconds=4)),timeout=5):
            raise ConnectionError('Temporal health unavailable')

    async def describe(self, workflow_id):
        try:
            return (await asyncio.wait_for(self.client.get_workflow_handle(workflow_id).describe(rpc_timeout=timedelta(seconds=5)),timeout=6)).status.name
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND: return None
            raise

    async def start(self, row):
        try:
            await asyncio.wait_for(self.client.start_workflow('newsagent-stream-snapshot-v1',row['snapshot_id'],id=row['workflow_id'],
                task_queue=self.queue,id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                execution_timeout=timedelta(minutes=6),rpc_timeout=timedelta(seconds=5)),timeout=6)
        except WorkflowAlreadyStartedError:
            pass


async def dispatch_once(ledger, transport):
    # Read-only health probe happens before reserving a logical submission.
    await transport.check()
    row=await asyncio.to_thread(ledger.claim)
    if row is None: return False
    try:
        status = None if row['has_report'] or row['invalidated'] else await transport.describe(row['workflow_id'])
        action,error=action_for(status=status,attempts=row['dispatch_attempts'],has_report=row['has_report'],invalidated=row['invalidated'])
        if action=='start':
            if not await asyncio.to_thread(ledger.reserve_start,row): return True
            await transport.start(row)
            action='started'
        await asyncio.to_thread(ledger.finish,row,action,error)
    except (RPCError, psycopg.OperationalError, psycopg.InterfaceError, ConnectionError, TimeoutError) as exc:
        # Leave uncertain start pending: the next owner must describe the stable ID.
        try: await asyncio.to_thread(ledger.finish,row,None,type(exc).__name__,5)
        except (psycopg.OperationalError,psycopg.InterfaceError): pass  # Lease expiry permits recovery after DB outage.
        raise
    return True


@dataclass
class DispatcherHealth:
    state: str = 'starting'
    last_success_at: str | None = None
    error_type: str | None = None
    consecutive_failures: int = 0
    last_tick: float = 0

    def public(self):
        fresh=self.last_tick>0 and time.monotonic()-self.last_tick<30
        return dict(state=self.state if fresh else 'stale',last_success_at=self.last_success_at,
                    error_type=self.error_type,consecutive_failures=self.consecutive_failures)


async def run_dispatcher(ledger,transport,health,*,sleep=asyncio.sleep):
    while True:
        try:
            worked=await dispatch_once(ledger,transport)
            health.state='ready';health.error_type=None;health.consecutive_failures=0
            health.last_success_at=datetime.now(timezone.utc).isoformat();health.last_tick=time.monotonic()
            await sleep(0.05 if worked else 1)
        except (RPCError,psycopg.OperationalError,psycopg.InterfaceError,ConnectionError,TimeoutError) as exc:
            health.state='recovering';health.error_type=type(exc).__name__
            health.consecutive_failures+=1;health.last_tick=time.monotonic()
            await sleep(min(15,2**min(health.consecutive_failures-1,4)))
        # Programming/schema errors propagate and stop the process; never hide them as retries.
