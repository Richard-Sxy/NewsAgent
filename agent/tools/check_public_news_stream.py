"""Read-only end-to-end recount: deterministic packet -> Flink -> report -> SQL join.

Supply LAB_DSN / LAB_UNIFIED_DSN and optionally LAB_TEMPO_URL explicitly.
Only metadata/counts/trace names are written to the acceptance report.
"""
import argparse
from hashlib import sha256
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from urllib.request import build_opener, ProxyHandler, Request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psycopg
from psycopg.rows import dict_row
from simulation.streaming_lab.public_news import load_catalog, public_packet, connect_target
from simulation.streaming_lab.dynamic_data import digest, TENANTS
from simulation.streaming_lab.bridge import validate_payload, FIELDS
from tools.streaming_lab_acceptance import recount


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    with psycopg.connect(os.environ['LAB_DSN'],row_factory=dict_row) as source,connect_target() as target:
        source.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        target.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        batch=source.execute('SELECT * FROM stream.dynamic_batches WHERE run_id=%s',(args.run_id,)).fetchone()
        assert batch and batch['state']=='completed' and batch['public_catalog_id']
        catalog=load_catalog(source,batch['public_catalog_id'])
        packet=public_packet(batch['sequence'],batch['window_start_ms'],catalog)
        assert digest(packet)==batch['packet_sha256']
        snapshots=source.execute('''SELECT s.*,r.report,l.state,d.delivered_at FROM stream.snapshots s
          JOIN stream.reports r USING(snapshot_id) JOIN stream.consumption_ledger l USING(snapshot_id)
          JOIN stream.warehouse_delivery d USING(snapshot_id) WHERE s.run_id=%s AND s.final''',(args.run_id,)).fetchall()
        assert len(snapshots)==2 and {r['tenant_id'] for r in snapshots}==set(TENANTS)
        report={'run_id':args.run_id,'catalog_id':catalog['catalog_id'],'checks':{},'snapshots':[],
            'generator_trace_id':batch['source_trace_id'],'source_mode':'synthetic','external_model_calls':0}
        for row in snapshots:
            body=validate_payload(row['payload_text'],row['snapshot_id'],row['tenant_id'],catalog=catalog)
            expected,count=recount(packet['events'],row['tenant_id'])
            assert expected==body['metrics'] and count==body['event_count']
            assert row['state']=='completed' and row['report']['external_model_calls']==0
            assert not source.execute('SELECT 1 FROM stream.invalid_windows WHERE window_key=%s',(row['window_key'],)).fetchone()
            stored=target.execute('SELECT * FROM news_realtime.snapshot WHERE snapshot_id=%s',(row['snapshot_id'],)).fetchone()
            assert stored and stored['payload_text']==row['payload_text'] and stored['report']==row['report']
            joined=target.execute('SELECT * FROM news_realtime.news_detail_current WHERE snapshot_id=%s ORDER BY news_id',(row['snapshot_id'],)).fetchall()
            assert len(joined)==3
            for item,metric in zip(joined,expected):
                assert item['news_id']==metric['news_id'] and item['source_check_fresh']
                assert all(item[k]==metric[k] for k in FIELDS)
                assert sha256(item['body'].encode()).hexdigest()==item['body_sha256']
                assert item['record_sha256']==catalog['news'][item['news_id']]['record_sha256']
                assert item['reader_profile_status']=='unavailable_for_this_minute_window'
                assert item['metric_source_mode']=='synthetic' and item['topic_ids'] is not None
                ranked=next(v for v in row['report']['ranking'] if v['news_id']==item['news_id'])
                assert item['hot_score']==Decimal(ranked['hot_score'])
            report['snapshots'].append({'snapshot_id':row['snapshot_id'],'tenant_id':row['tenant_id'],
                'workflow_id':row['report']['workflow_id'],'analysis_trace_id':row['report']['trace_id'],
                'unique_events':count,'news_ids':[r['news_id'] for r in joined],
                'projection_seconds':float((stored['projected_at']-stored['source_published_at']).total_seconds())})
        report['checks']={k:True for k in ('packet_replay','independent_event_recount','both_tenants','flink_final',
            'report_metrics','durable_delivery','sql_content_hash','sql_content_tags','source_freshness','no_stale_reader_profile')}
    if os.environ.get('LAB_TEMPO_URL'):
        http=build_opener(ProxyHandler({}))
        report['traces']=[]
        for trace_id in [report['generator_trace_id'],*[r['analysis_trace_id'] for r in report['snapshots']]]:
            request=Request(os.environ['LAB_TEMPO_URL'].rstrip('/')+'/api/traces/'+trace_id,headers={'Accept':'application/json'})
            with http.open(request,timeout=10) as response: payload=json.load(response)
            spans=[s for batch in payload.get('batches',payload.get('resourceSpans',[]))
                   for scope in batch.get('scopeSpans',batch.get('instrumentationLibrarySpans',[])) for s in scope.get('spans',[])]
            assert spans,'Trace not retrievable: '+trace_id
            report['traces'].append({'trace_id':trace_id,'span_names':sorted({s['name'] for s in spans}),
                                    'span_count':len(spans)})
    report['status']='passed'
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
