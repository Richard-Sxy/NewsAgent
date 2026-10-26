"""Read-only replay of a completed SQL window through the enterprise retrieval Port.

No Kafka writes, workflow dispatch, report replacement, or synthetic user export.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from simulation.streaming_lab.bridge import analyze_snapshot
from simulation.streaming_lab.dynamic_data import TENANTS
from simulation.streaming_lab.public_news import (
    PublicContentRepository, connect_target, content_engine, load_catalog,
)
from simulation.streaming_lab.reader_port import service
from simulation.streaming_lab.sql_retrieval import SqlRelatedNewsSearchClient


async def replay(*, tenant_id, snapshot_id=None):
    if tenant_id not in TENANTS:
        raise ValueError('Unknown local business tenant')
    with connect_target() as target, target.transaction():
        target.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        row=target.execute('''SELECT s.* FROM news_realtime.snapshot s
            JOIN news_realtime.reader_final r USING(window_key)
            WHERE s.tenant_id=%s AND (%s::text IS NULL OR s.snapshot_id=%s)
              AND NOT EXISTS (SELECT 1 FROM news_realtime.invalid_window i WHERE i.window_key=s.window_key)
            ORDER BY s.window_start_ms DESC,s.revision DESC LIMIT 1''',
            (tenant_id,snapshot_id,snapshot_id)).fetchone()
        if not row:
            raise ValueError('No completed, reconciled SQL window available')
        audience=target.execute('SELECT * FROM news_realtime.reader_final WHERE window_key=%s',
                                (row['window_key'],)).fetchone()
    with psycopg.connect(os.environ['LAB_DSN'],row_factory=dict_row,connect_timeout=5,
                         options='-c default_transaction_read_only=on -c statement_timeout=10000') as source:
        original=source.execute('''SELECT s.* FROM stream.snapshots s WHERE snapshot_id=%s
            AND final AND NOT EXISTS (SELECT 1 FROM stream.invalid_windows i WHERE i.window_key=s.window_key)''',
            (row['snapshot_id'],)).fetchone()
        if not original or original['payload_text']!=row['payload_text']:
            raise ValueError('Source snapshot missing, invalidated or changed')
        body=json.loads(row['payload_text'])
        catalog=load_catalog(source,body['catalog_id'])
        session=source.execute('SELECT * FROM stream.wallclock_session WHERE id=%s',
                               (audience['session_id'],)).fetchone()
    if not session:
        raise ValueError('Frozen reader session missing')
    engine=content_engine()
    readers=service(session,audience)
    try:
        report=await analyze_snapshot(row['payload_text'],row['snapshot_id'],tenant_id,
            catalog=catalog,content_repository=PublicContentRepository(catalog,engine),reader_service=readers,
            related_search=SqlRelatedNewsSearchClient(engine,catalog,tenant_id=tenant_id,as_of_ms=body['window_end_ms']))
    finally:
        await readers.close()
        engine.dispose()
    # A conflict arriving during replay must not turn into a successful artifact.
    with connect_target() as target, target.transaction():
        target.execute('SET TRANSACTION READ ONLY')
        invalid=target.execute('SELECT 1 FROM news_realtime.invalid_window WHERE window_key=%s',
                               (row['window_key'],)).fetchone()
    with psycopg.connect(os.environ['LAB_DSN'],connect_timeout=5,
                         options='-c default_transaction_read_only=on -c statement_timeout=10000') as source:
        invalid_source=source.execute('SELECT 1 FROM stream.invalid_windows WHERE window_key=%s',
                                      (row['window_key'],)).fetchone()
    if invalid or invalid_source:
        raise ValueError('Window invalidated during replay')
    return dict(validation_mode='readonly_historical_replay',replayed_at=datetime.now(timezone.utc).isoformat(),
        window_start_ms=body['window_start_ms'],window_end_ms=body['window_end_ms'],
        eligible_public_news=len(catalog['news']),report=report)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tenant-id',choices=TENANTS,default=TENANTS[0])
    parser.add_argument('--snapshot-id')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=asyncio.run(replay(tenant_id=args.tenant_id,snapshot_id=args.snapshot_id))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    report=result['report']
    print(json.dumps(dict(mode=result['validation_mode'],snapshot_id=report['snapshot_id'],
        run_key=report['run_key'],articles=result['eligible_public_news'],analyses=len(report['analyses']),
        reader_status=report['reader_profile_status'],
        candidates=[len(r['candidates']) for call in report['retrieval']['calls'] for r in call['results']],
        accepted_evidence=[len(a['retrieval_evidence']) for a in report['analyses']],
        sql_elapsed_ms=[c['elapsed_ms'] for c in report['retrieval']['calls']],output=str(args.output)),ensure_ascii=False))


if __name__=='__main__':
    main()
