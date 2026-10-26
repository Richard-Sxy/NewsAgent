"""Deduplicate/import public sources, register SQL catalogs, or enable the local feed.

Requires explicit UNIFIED_NEWS_DATABASE_URL, LAB_UNIFIED_DSN, LAB_DSN.
No Settings/.env, external models or enterprise personal events.
"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psycopg
from psycopg.rows import dict_row
from sqlalchemy import create_engine
from app.modules.news_storage.model import build_package
from app.modules.news_storage.sources import load_existing_sources, source_summary
from app.modules.news_storage.package_io import load_package
from app.modules.news_storage.topic_labels import load_tag_package
from app.modules.news_storage.store import FrozenNewsStore
from app.modules.news_storage.tag_store import FrozenNewsTagStore
from simulation.streaming_lab.public_news import connect_target, initialize_target, register_catalog, load_catalog, project_once


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['prepare','activate','project'])
    parser.add_argument('--root',type=Path)
    parser.add_argument('--package',type=Path)
    parser.add_argument('--tags-package',type=Path)
    parser.add_argument('--catalog-id')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    with psycopg.connect(os.environ['LAB_DSN'],autocommit=True,row_factory=dict_row) as source:
        if args.command=='prepare':
            if bool(args.root)==bool(args.package) or not args.tags_package:
                parser.error('prepare requires exactly one --root or --package, plus --tags-package')
            inventory=None
            if args.root:
                candidates=load_existing_sources(args.root)
                inventory=source_summary(candidates)
                package=build_package(candidates,tenant_id='public-news-local')
            else:
                package=load_package(args.package)
            tags=load_tag_package(args.tags_package)
            engine=create_engine(os.environ['UNIFIED_NEWS_DATABASE_URL'])
            try:
                store=FrozenNewsStore(engine)
                content_result=store.import_release(package)
                tag_result=FrozenNewsTagStore(store).import_release(tags)
            finally:
                engine.dispose()
            with connect_target() as target:
                initialize_target(target)
                result=register_catalog(target,source,content_tenant=package['manifest']['tenant_id'],
                    content_release=package['manifest']['release_id'],tag_release=tags['manifest']['tag_release_id'])
                counts=target.execute('''SELECT count(*) AS news_ids,count(*) FILTER(WHERE news_id=canonical_news_id) AS canonical_news,
                  count(*) FILTER(WHERE news_id<>canonical_news_id) AS aliases FROM public.news_content_dedup_map
                  WHERE tenant_id=%s AND release_id=%s''',(package['manifest']['tenant_id'],package['manifest']['release_id'])).fetchone()
            result.update(content_import=content_result,tag_import=tag_result,inventory=inventory,dedup=counts)
        elif args.command=='activate':
            if not args.catalog_id: parser.error('--catalog-id required')
            with source.transaction():
                source.execute('SELECT pg_advisory_xact_lock(480004)')
                load_catalog(source,args.catalog_id)
                with connect_target() as target:
                    load_catalog(target,args.catalog_id,target=True)
                feed=source.execute('SELECT * FROM stream.dynamic_feed WHERE id=1 FOR UPDATE').fetchone()
                if feed['enabled'] or source.execute("SELECT 1 FROM stream.dynamic_batches WHERE state<>'completed' LIMIT 1").fetchone():
                    raise ValueError('Pause and drain the existing producer before switching catalog')
                source.execute('UPDATE stream.dynamic_feed SET public_catalog_id=%s WHERE id=1',(args.catalog_id,))
            result={'catalog_id':args.catalog_id,'state':'selected_feed_still_paused'}
        else:
            with connect_target() as target:
                result={'delivered':project_once(source,target)}
    text=json.dumps(result,ensure_ascii=False,indent=2,default=str)+'\n'
    if args.output: args.output.write_text(text,encoding='utf-8')
    print(text,end='')


if __name__=='__main__':
    main()
