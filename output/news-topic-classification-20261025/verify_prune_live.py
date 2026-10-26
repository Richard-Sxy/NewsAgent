"""Read-only acceptance of the approved prune against the running local stores."""
import asyncio
import json
import os
from pathlib import Path
import sys
from urllib.request import urlopen

import psycopg
from psycopg.rows import dict_row
from sqlalchemy import create_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'agent'))
from simulation.streaming_lab.public_news import PublicContentRepository, load_catalog


def main():
    output=Path(sys.argv[1])
    expected=json.loads((output/'selection.json').read_text())
    decisions=[json.loads(line) for line in (output/'decisions.jsonl').read_text().splitlines()]
    removed={r['news_id'] for r in decisions if r['action']=='remove_other'}
    kept={r['news_id']:r['record_sha256'] for r in decisions if r['action']!='remove_other'}
    options='-c default_transaction_read_only=on -c statement_timeout=60000'
    with psycopg.connect(os.environ['LAB_UNIFIED_DSN'],row_factory=dict_row,options=options) as target, psycopg.connect(os.environ['LAB_DSN'],row_factory=dict_row,options=options) as source:
        selected=target.execute('SELECT news_id,record_sha256 FROM news_content_selected WHERE tenant_id=%s',(expected['tenant_id'],)).fetchall()
        assert len(selected)==16369 and {r['news_id']:r['record_sha256'] for r in selected}==kept
        versions=target.execute('SELECT count(*) AS n FROM news_content_versions WHERE tenant_id=%s AND news_id=ANY(%s)',(expected['tenant_id'],sorted(removed))).fetchone()['n']
        assert versions==0
        categories=target.execute('SELECT category_status,target_category,count(*) AS count FROM news_content_current_categories GROUP BY 1,2 ORDER BY 1,2').fetchall()
        held_ids={r['news_id'] for r in decisions if r['action'].startswith('hold_')}
        held_or_annotated=target.execute("SELECT news_id FROM news_content_current_categories WHERE category_status LIKE 'hold_%%' OR category_status='auto_labeled'").fetchall()
        assert len(held_or_annotated)==4714 and {r['news_id'] for r in held_or_annotated}==held_ids
        release_states=target.execute('SELECT release_id,status FROM news_content_releases WHERE tenant_id=%s ORDER BY release_id',(expected['tenant_id'],)).fetchall()
        assert {r['release_id']:r['status'] for r in release_states}=={expected['source_release_id']:'archived',expected['content_release_id']:'frozen'}
        views={}
        for name in ('news_detail_current','news_live_current'):
            views[name]=target.execute('SELECT count(*) AS n FROM news_realtime.'+name+' WHERE news_id=ANY(%s)',(sorted(removed),)).fetchone()['n']
            assert views[name]==0
        feed=source.execute('SELECT enabled,public_catalog_id FROM stream.dynamic_feed WHERE id=1').fetchone()
        assert not feed['enabled'] and not source.execute('SELECT 1 FROM stream.wallclock_session WHERE enabled').fetchone()
        catalog=load_catalog(source,feed['public_catalog_id'])
        assert catalog['manifest']['content_release_id']==expected['content_release_id']
        assert len(catalog['news'])==6486 and not (set(catalog['news']) & removed)
        affected=source.execute('''SELECT count(DISTINCT s.window_key) AS n FROM stream.snapshots s
            JOIN stream.public_catalog c ON c.catalog_id=((s.payload_text::jsonb)->>'catalog_id')
            WHERE c.manifest->>'content_release_id'=%s AND c.manifest->>'content_tenant_id'=%s
              AND EXISTS(SELECT 1 FROM jsonb_array_elements((s.payload_text::jsonb)->'metrics') m WHERE m->>'news_id'=ANY(%s))
              AND NOT EXISTS(SELECT 1 FROM stream.invalid_windows i WHERE i.window_key=s.window_key)''',
              (expected['source_release_id'],expected['tenant_id'],sorted(removed))).fetchone()['n']
        assert affected==0
        old_catalog=load_catalog(source,'public-ff7fd27af0eac7e217234e93')
        engine=create_engine(os.environ['LAB_UNIFIED_DSN'].replace('postgresql://','postgresql+psycopg://',1),connect_args={'options':options},hide_parameters=True)
        repository=PublicContentRepository(old_catalog,engine)
        assert repository.content_binding['content_release_id']==expected['content_release_id']
        kept_id='20260718A03B9V00'
        removed_id='20260819A0762Q00'
        assert kept_id in kept and removed_id in removed
        rows=asyncio.run(repository.batch_get_by_news_ids(tenant_id=old_catalog['manifest']['tenants'][0],news_ids=[kept_id]))
        assert len(rows)==1
        try:
            asyncio.run(repository.batch_get_by_news_ids(tenant_id=old_catalog['manifest']['tenants'][0],news_ids=[removed_id]))
        except ValueError:
            pass
        else:
            raise AssertionError('Removed article resolved through a historical catalog')
        engine.dispose()
    api={}
    for endpoint in ('ready','streaming-lab-api/public-news','streaming-lab-api/realtime-news'):
        suffix='' if endpoint=='ready' else '?tenant_id=11111111-1111-4111-8111-111111111111&limit=100'
        with urlopen('http://host.docker.internal:48000/'+endpoint+suffix,timeout=30) as response:
            value=json.load(response)
        if 'items' in value:
            assert not ({r['news_id'] for r in value['items']} & removed)
            api[endpoint]={'ok':True,'returned_count':len(value['items']),'removed_news_returned':0}
        else:
            assert value['status']=='ok'
            api[endpoint]=value
    result={'status':'passed','operation_id':expected['operation_id'],'active_news':len(selected),
            'removed_ids_remaining_in_versions':versions,'categories':categories,'release_states':release_states,
            'removed_ids_in_realtime_views':views,'feed':feed,'eligible_catalog_count':len(catalog['news']),
            'affected_windows_missing_invalidation':affected,'historical_retained_read':True,
            'historical_removed_read_rejected':True,'api':api}
    (output/'live-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
