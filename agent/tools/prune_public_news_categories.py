"""Versioned, backed-up pruning of the explicitly approved local news collection.

Build a retained frozen release; apply it to the unified DB and paused stream.
Only the approved conservative-remove IDs may be deleted. Source crawl caches,
unrelated databases and historical vector snapshots are outside this operation.
No .env discovery, model call, training, feed start or immutable-trigger bypass.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.modules.news_storage import model, topic_labels
from app.modules.news_storage.package_io import load_package, save_package

OPERATION = 'news-category-prune-conservative-20261025-v1'
OLD_RELEASE = 'news-0c74530f2864db20a73d7a96'
OLD_TAGS = 'tags-33c4054fd01bd98cd5ef2826'
TENANT = 'public-news-local'
BACKUP_SHA = '475111c9bf41a744def33b9b3e0a4e13a837acb7bbdd7539c797a295b0040cfe'
EXPECTED_REMOVED = 5066
EXPECTED_RETAINED = 16369


def read_rows(path):
    with Path(path).open(encoding='utf8') as stream:
        return [json.loads(line) for line in stream]


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str)+'\n', encoding='utf8')


def subset_packages(content, tags, retained_ids):
    """Rebind a subset to fresh release identities without changing retained text."""
    model.validate_package(content)
    topic_labels.validate_tag_package(tags, content, verify_derivation=False)
    known = {item['news_id'] for item in content['items']}
    if not retained_ids or not retained_ids < known:
        raise ValueError('Expected a nonempty proper subset of the frozen content')
    new_content = deepcopy(content)
    for key in ('versions', 'provenance', 'items'):
        new_content[key] = [r for r in new_content[key] if r['news_id'] in retained_ids]
    cm = new_content['manifest']
    identity = model.digest(model._identity(new_content))
    cm.update(release_id='news-'+identity[:24], identity_sha256=identity,
              counts=model._counts(new_content))
    model.validate_package(new_content)
    new_tags = deepcopy(tags)
    new_tags['labels'] = [r for r in new_tags['labels'] if r['news_id'] in retained_ids]
    tm = new_tags['manifest']
    tm.update(content_release_id=cm['release_id'], content_identity_sha256=identity)
    tag_identity = model.digest(topic_labels._identity(new_tags))
    tm.update(identity_sha256=tag_identity, tag_release_id='tags-'+tag_identity[:24],
              counts=topic_labels._counts(new_tags['labels']))
    topic_labels.validate_tag_package(new_tags, new_content, verify_derivation=False)
    return new_content, new_tags


def build(root, output):
    if output.exists():
        raise FileExistsError('Choose a new output directory')
    report = root/'output/news-topic-classification-20261025'
    plan = report/'prune-plan'
    for name, checksum in json.loads((plan/'manifest.json').read_text())['files'].items():
        if Path(name).name != name or sha256((plan/name).read_bytes()).hexdigest() != checksum:
            raise ValueError('Prune plan checksum mismatch')
    decisions = read_rows(plan/'decisions.jsonl')
    removed = {r['news_id'] for r in decisions if r['action']=='remove_other'}
    retained = {r['news_id'] for r in decisions if r['action']!='remove_other'}
    if (len(removed), len(retained)) != (EXPECTED_REMOVED, EXPECTED_RETAINED):
        raise ValueError('Counts differ from the user-approved conservative plan')
    if removed != {r['news_id'] for r in read_rows(plan/'conservative-remove.jsonl')}:
        raise ValueError('Removal list differs from decisions')
    backup = root/'var/backups/news-category-prune-20261025/unified-news.before.dump'
    if sha256(backup.read_bytes()).hexdigest() != BACKUP_SHA:
        raise ValueError('Recovery backup checksum mismatch')
    content = load_package(root/'var/news-storage/unified-public-news-20261023')
    tags = topic_labels.load_tag_package(root/'var/news-storage/tags/public-news-topics-20261023')
    if content['manifest']['release_id'] != OLD_RELEASE or tags['manifest']['tag_release_id'] != OLD_TAGS:
        raise ValueError('Unexpected source release')
    original = {r['news_id']:r['record_sha256'] for r in content['items']}
    if original != {r['news_id']:r['record_sha256'] for r in decisions}:
        raise ValueError('Classification decisions do not cover exact selected content')
    new_content, new_tags = subset_packages(content, tags, retained)
    output.mkdir(parents=True)
    save_package(new_content, output/'content')
    topic_labels.save_tag_package(new_tags, output/'tags')
    (output/'decisions.jsonl').write_bytes((plan/'decisions.jsonl').read_bytes())
    exported = read_rows(root/'var/news-storage/exports/20261023/all-news-with-tags.jsonl')
    if {r['news_id']:r['record_sha256'] for r in exported} != original:
        raise ValueError('Export source differs from frozen input')
    with (output/'all-news-with-tags.jsonl').open('x', encoding='utf8') as handle:
        for row in exported:
            if row['news_id'] not in retained:
                continue
            row['release_id'] = new_content['manifest']['release_id']
            row['tags']['tag_release_id'] = new_tags['manifest']['tag_release_id']
            handle.write(model.canonical_json(row)+'\n')
    summary = dict(operation_id=OPERATION, status='prepared', tenant_id=TENANT,
                   source_release_id=OLD_RELEASE, source_tag_release_id=OLD_TAGS,
                   content_release_id=new_content['manifest']['release_id'],
                   tag_release_id=new_tags['manifest']['tag_release_id'],
                   source_selected_sha256=model.digest(original),
                   decision_sha256=sha256((output/'decisions.jsonl').read_bytes()).hexdigest(),
                   export_sha256=sha256((output/'all-news-with-tags.jsonl').read_bytes()).hexdigest(),
                   backup_sha256=BACKUP_SHA, removed_count=len(removed), retained_count=len(retained),
                   retained_counts=new_content['manifest']['counts'],
                   target_categories=dict(Counter(r['target_category_name'] for r in decisions if r['action']=='keep_target')),
                   held_for_review=sum(r['action'].startswith('hold_') for r in decisions),
                   authorization='User explicitly chose to retain uncertain records and remove only clear other categories.')
    write_json(output/'selection.json', summary)
    return summary


def verify(target, content, tags, decisions):
    removed = {r['news_id'] for r in decisions if r['action']=='remove_other'}
    retained = {r['news_id']:r['record_sha256'] for r in decisions if r['action']!='remove_other'}
    visible = target.execute('SELECT news_id,record_sha256 FROM public.news_content_selected WHERE tenant_id=%s', (TENANT,)).fetchall()
    if {r['news_id']:r['record_sha256'] for r in visible} != retained or len(visible) != len(retained):
        raise ValueError('Active content differs from approved retention set')
    remains = target.execute('SELECT count(*) AS n FROM public.news_content_versions WHERE tenant_id=%s AND news_id=ANY(%s)',
                             (TENANT, sorted(removed))).fetchone()['n']
    if remains:
        raise ValueError('Removed content versions still present')
    tagged = target.execute('SELECT count(*) AS n FROM public.news_content_tagged_selected WHERE tenant_id=%s', (TENANT,)).fetchone()['n']
    if tagged != len(retained):
        raise ValueError('Retained content and visible tags differ')
    return {'active_news':len(visible), 'active_tagged_news':tagged,
            'removed_ids_remaining_in_versions':remains,
            'content_release_id':content['manifest']['release_id'],
            'tag_release_id':tags['manifest']['tag_release_id']}


def invalidate_removed_windows(source, target, removed, output):
    """Retire whole immutable metric windows rather than rewrite their counters."""
    before=output/'source-before.json'
    if not before.exists():
        write_json(before, {'operation_id':OPERATION,
                           'dynamic_feed':source.execute('SELECT id,enabled,public_catalog_id FROM stream.dynamic_feed WHERE id=1').fetchone(),
                           'wallclock':source.execute('SELECT id,enabled,start_ms,last_error FROM stream.wallclock_session ORDER BY start_ms DESC LIMIT 1').fetchone(),
                           'restore_scope':'Restore this catalog pointer while paused; remove invalid_windows entries with the exact operation reason only if restoring the original content backup.'})
    source.execute('''INSERT INTO stream.invalid_windows(window_key,reason,observed_at)
        SELECT window_key,%s,clock_timestamp() FROM (
            SELECT DISTINCT s.window_key FROM stream.snapshots s JOIN stream.public_catalog c
              ON c.catalog_id=((s.payload_text::jsonb)->>'catalog_id')
            WHERE c.manifest->>'content_release_id'=%s AND c.manifest->>'content_tenant_id'=%s
              AND EXISTS(SELECT 1 FROM jsonb_array_elements((s.payload_text::jsonb)->'metrics') m
                         WHERE m->>'news_id'=ANY(%s))) affected
        ON CONFLICT DO NOTHING''',(OPERATION,OLD_RELEASE,TENANT,[r['news_id'] for r in removed]))
    rows=source.execute('SELECT window_key,reason,observed_at FROM stream.invalid_windows WHERE reason=%s',(OPERATION,)).fetchall()
    with target.cursor() as cur:
        cur.executemany('INSERT INTO news_realtime.invalid_window(window_key,reason,observed_at) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',
                        [(r['window_key'],r['reason'],r['observed_at']) for r in rows])
    return len(rows)


def apply(output, database_env, source_env, validation):
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    from sqlalchemy import create_engine
    from app.modules.news_storage.store import FrozenNewsStore
    from app.modules.news_storage.tag_store import FrozenNewsTagStore
    from simulation.streaming_lab.public_news import register_catalog, load_catalog

    selection = json.loads((output/'selection.json').read_text())
    decisions = read_rows(output/'decisions.jsonl')
    if selection['operation_id'] != OPERATION or sha256((output/'decisions.jsonl').read_bytes()).hexdigest() != selection['decision_sha256']:
        raise ValueError('Plan identity mismatch')
    removed = [r for r in decisions if r['action']=='remove_other']
    if len(removed) != EXPECTED_REMOVED or selection['backup_sha256'] != BACKUP_SHA:
        raise ValueError('Unapproved removal count or missing recovery backup binding')
    content = load_package(output/'content')
    tags = topic_labels.load_tag_package(output/'tags')
    topic_labels.validate_tag_package(tags, content, verify_derivation=False)
    if content['manifest']['release_id'] != selection['content_release_id'] or tags['manifest']['tag_release_id'] != selection['tag_release_id']:
        raise ValueError('Prepared release identity mismatch')
    target_dsn = os.environ[database_env]
    source_dsn = os.environ[source_env] if not validation else target_dsn
    engine = create_engine(target_dsn.replace('postgresql://','postgresql+psycopg://',1), hide_parameters=True)
    with psycopg.connect(target_dsn, row_factory=dict_row, autocommit=True) as target, psycopg.connect(source_dsn, row_factory=dict_row, autocommit=True) as source:
        if not target.execute('SELECT pg_try_advisory_lock(480025) AS ok').fetchone()['ok']:
            raise RuntimeError('Another pruning operation is active')
        if not validation:
            if source.execute('SELECT enabled FROM stream.dynamic_feed WHERE id=1').fetchone()['enabled']:
                raise ValueError('The source must already be paused')
            if source.execute('SELECT 1 FROM stream.wallclock_session WHERE enabled LIMIT 1').fetchone():
                raise ValueError('Wall-clock source must already be paused')
            if source.execute("SELECT 1 FROM stream.dynamic_batches WHERE state<>'completed' LIMIT 1").fetchone():
                raise ValueError('Outstanding dynamic batch must be drained before pruning')
        old = target.execute('SELECT status FROM public.news_content_releases WHERE tenant_id=%s AND release_id=%s',(TENANT,OLD_RELEASE)).fetchone()
        if old is None or old['status'] not in {'frozen','archived'}:
            raise ValueError('Unexpected original release state')
        invalidated_windows=0
        if old['status']=='frozen':
            original = target.execute('SELECT news_id,record_sha256 FROM public.news_content_release_items WHERE tenant_id=%s AND release_id=%s',(TENANT,OLD_RELEASE)).fetchall()
            if model.digest({r['news_id']:r['record_sha256'] for r in original}) != selection['source_selected_sha256']:
                raise ValueError('Database content changed since approval')
            if not validation:
                invalidated_windows=invalidate_removed_windows(source,target,removed,output)
            store = FrozenNewsStore(engine)
            store.import_release(content)
            FrozenNewsTagStore(store).import_release(tags)
            with target.transaction():
                target.execute('LOCK TABLE public.news_content_releases, public.news_content_versions, public.news_content_release_items, public.news_content_provenance, public.news_content_tag_releases, public.news_content_tags, public.news_content_topic_memberships IN SHARE ROW EXCLUSIVE MODE')
                # An archived header preserves the original identity for audit; it
                # must no longer claim its pruned rows form a complete frozen set.
                target.execute('CREATE TABLE IF NOT EXISTS public.news_content_prune_runs (operation_id text PRIMARY KEY, source_release_id text NOT NULL, target_release_id text NOT NULL, manifest jsonb NOT NULL, applied_at timestamptz NOT NULL DEFAULT clock_timestamp())')
                target.execute('CREATE TABLE IF NOT EXISTS public.news_content_prune_decisions (operation_id text NOT NULL REFERENCES public.news_content_prune_runs, news_id text NOT NULL, record_sha256 text NOT NULL, action text NOT NULL, target_category text, review_flags jsonb NOT NULL, PRIMARY KEY(operation_id,news_id))')
                target.execute('INSERT INTO public.news_content_prune_runs(operation_id,source_release_id,target_release_id,manifest) VALUES(%s,%s,%s,%s)',(OPERATION,OLD_RELEASE,selection['content_release_id'],Jsonb(selection)))
                with target.cursor() as cur, cur.copy('COPY public.news_content_prune_decisions(operation_id,news_id,record_sha256,action,target_category,review_flags) FROM STDIN') as copy:
                    for row in decisions:
                        copy.write_row((OPERATION,row['news_id'],row['record_sha256'],row['action'],row['target_category'],Jsonb(row['review_flags'])))
                target.execute('CREATE TEMP TABLE prune_remove(news_id text PRIMARY KEY) ON COMMIT DROP')
                with target.cursor() as cur, cur.copy('COPY prune_remove(news_id) FROM STDIN') as copy:
                    for row in removed:
                        copy.write_row((row['news_id'],))
                other_refs = target.execute('SELECT count(*) AS n FROM public.news_content_release_items WHERE tenant_id=%s AND release_id<>%s AND news_id IN(SELECT news_id FROM prune_remove)',(TENANT,OLD_RELEASE)).fetchone()['n']
                if other_refs:
                    raise ValueError('Removal IDs are referenced by another collection')
                # Retained reader bindings point to the new content identity too;
                # historical aggregate facts are neither resummed nor rewritten.
                target.execute('''INSERT INTO news_audience.unified_news_binding(tenant_id,source_release_sha256,news_id,content_tenant_id,content_release_id,record_sha256,body_sha256,binding_method)
                    SELECT b.tenant_id,b.source_release_sha256,b.news_id,b.content_tenant_id,%s,b.record_sha256,b.body_sha256,b.binding_method
                    FROM news_audience.unified_news_binding b JOIN public.news_content_release_items i
                    ON i.tenant_id=b.content_tenant_id AND i.release_id=%s AND i.news_id=b.news_id AND i.record_sha256=b.record_sha256
                    WHERE b.content_tenant_id=%s AND b.content_release_id=%s ON CONFLICT DO NOTHING''',
                    (selection['content_release_id'],selection['content_release_id'],TENANT,OLD_RELEASE))
                deleted = {}
                for table, release_column in [('news_content_topic_memberships','content_release_id'),('news_content_tags','content_release_id'),('news_content_provenance','release_id'),('news_content_release_items','release_id')]:
                    deleted[table] = target.execute(f'DELETE FROM public.{table} WHERE tenant_id=%s AND {release_column}=%s AND news_id IN(SELECT news_id FROM prune_remove)',(TENANT,OLD_RELEASE)).rowcount
                deleted['audience_bindings'] = target.execute('DELETE FROM news_audience.unified_news_binding WHERE content_tenant_id=%s AND content_release_id=%s AND news_id IN(SELECT news_id FROM prune_remove)',(TENANT,OLD_RELEASE)).rowcount
                deleted['content_versions'] = target.execute('DELETE FROM public.news_content_versions WHERE tenant_id=%s AND news_id IN(SELECT news_id FROM prune_remove)',(TENANT,)).rowcount
                target.execute("UPDATE public.news_content_releases SET status='archived' WHERE tenant_id=%s AND release_id=%s",(TENANT,OLD_RELEASE))
                target.execute("UPDATE public.news_content_tag_releases SET status='archived' WHERE tenant_id=%s AND content_release_id=%s",(TENANT,OLD_RELEASE))
                # Historical reports can still resolve retained exact versions;
                # active collection views continue to expose only frozen releases.
                target.execute('''CREATE OR REPLACE VIEW public.news_content_retained_history AS
                    SELECT r.tenant_id,r.release_id,r.identity_sha256,v.news_id,v.record_sha256,v.body_sha256,
                    v.title,v.body,v.url,v.publish_time,v.content_type,v.content_state,i.quality_flags AS item_quality_flags,v.record
                    FROM public.news_content_releases r JOIN public.news_content_release_items i USING(tenant_id,release_id)
                    JOIN public.news_content_versions v ON v.tenant_id=i.tenant_id AND v.news_id=i.news_id AND v.record_sha256=i.record_sha256
                    WHERE r.status='frozen' OR (r.status='archived' AND EXISTS(SELECT 1 FROM public.news_content_prune_runs p WHERE p.source_release_id=r.release_id))''')
                for name in ('news_detail_current','news_live_current'):
                    definition = target.execute('SELECT pg_get_viewdef(%s::regclass,true) AS sql',('news_realtime.'+name,)).fetchone()['sql']
                    if len(re.findall(r'\bnews_content_selected\b',definition)) != 1:
                        raise ValueError('Historical view changed; inspect before applying')
                    definition = re.sub(r'\bnews_content_selected\b','news_content_retained_history',definition)
                    target.execute('CREATE OR REPLACE VIEW news_realtime.'+name+' AS '+definition)
                target.execute('''CREATE OR REPLACE VIEW public.news_content_current_categories AS
                    SELECT c.*,d.target_category,d.action AS category_status,d.review_flags
                    FROM public.news_content_selected c JOIN public.news_content_prune_runs p ON p.target_release_id=c.release_id
                    JOIN public.news_content_prune_decisions d ON d.operation_id=p.operation_id AND d.news_id=c.news_id AND d.record_sha256=c.record_sha256
                    WHERE d.action<>'remove_other' ''')
                checks = verify(target,content,tags,decisions)
                target.execute('UPDATE public.news_content_prune_runs SET manifest=manifest || %s WHERE operation_id=%s',
                               (Jsonb({'status':'applied_target','deleted_rows':deleted,'post_delete_checks':checks}),OPERATION))
        else:
            operation = target.execute('SELECT target_release_id FROM public.news_content_prune_runs WHERE operation_id=%s',(OPERATION,)).fetchone()
            if not operation or operation['target_release_id'] != selection['content_release_id']:
                raise ValueError('Archived source was not produced by this approved operation')
            checks = verify(target,content,tags,decisions)
            deleted = target.execute("SELECT manifest->'deleted_rows' AS rows FROM public.news_content_prune_runs WHERE operation_id=%s",(OPERATION,)).fetchone()['rows']
            if not validation:
                invalidated_windows=invalidate_removed_windows(source,target,removed,output)
        # Native full-package round trip confirms the new frozen identities and
        # inferred-label contracts remain valid after physical removal.
        store = FrozenNewsStore(engine)
        with engine.connect() as connection:
            model.validate_package(store._read_package(connection,TENANT,selection['content_release_id']))
            topic_labels.validate_tag_package(FrozenNewsTagStore(store)._read_package(connection,TENANT,selection['content_release_id'],selection['tag_release_id']),content,verify_derivation=False)
        if validation:
            source.execute('CREATE SCHEMA IF NOT EXISTS stream')
            source.execute('CREATE TABLE IF NOT EXISTS stream.public_catalog (LIKE news_realtime.catalog INCLUDING ALL)')
            source.execute('CREATE TABLE IF NOT EXISTS stream.public_news (LIKE news_realtime.catalog_news INCLUDING ALL)')
        catalog = register_catalog(target,source,content_tenant=TENANT,content_release=selection['content_release_id'],tag_release=selection['tag_release_id'])
        loaded = load_catalog(source,catalog['catalog_id'])
        if set(loaded['news']) & {r['news_id'] for r in removed}:
            raise ValueError('Pruned content reappeared in the new feed catalog')
        if not validation:
            with source.transaction():
                source.execute('SELECT pg_advisory_xact_lock(480004)')
                feed = source.execute('SELECT enabled FROM stream.dynamic_feed WHERE id=1 FOR UPDATE').fetchone()
                if feed['enabled'] or source.execute('SELECT 1 FROM stream.wallclock_session WHERE enabled LIMIT 1').fetchone():
                    raise ValueError('Feed resumed during maintenance; new catalog is prepared but not selected')
                source.execute('UPDATE stream.dynamic_feed SET public_catalog_id=%s WHERE id=1',(catalog['catalog_id'],))
        target.execute('UPDATE public.news_content_prune_runs SET manifest=manifest || %s WHERE operation_id=%s',
                       (Jsonb({'status':'applied_verified','catalog_id':catalog['catalog_id']}),OPERATION))
        result = {'operation_id':OPERATION,'validation_database_only':validation,'status':'applied_verified',
                  'checks':checks,'deleted_rows':deleted,'catalog':catalog,'feed_state':'paused',
                  'invalidated_historical_windows':invalidated_windows,
                  'held_for_review':selection['held_for_review'],'backup_sha256':BACKUP_SHA,
                  'scope':'Current unified content database and its selected public feed catalog. Original crawl caches, frozen disk inputs and separate historical vector/warehouse datasets are retained as independent sources.'}
        write_json(output/('validation-result.json' if validation else 'apply-result.json'),result)
        engine.dispose()
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['build','apply'])
    parser.add_argument('--root',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--database-env',default='LAB_UNIFIED_DSN')
    parser.add_argument('--source-env',default='LAB_DSN')
    parser.add_argument('--validation',action='store_true')
    args=parser.parse_args()
    if args.command=='build':
        if not args.root: parser.error('--root is required for build')
        result=build(args.root,args.output)
    else:
        result=apply(args.output,args.database_env,args.source_env,args.validation)
    print(json.dumps(result,ensure_ascii=False,indent=2,default=str))


if __name__=='__main__':
    main()
