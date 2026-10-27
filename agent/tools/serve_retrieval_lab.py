"""Build and serve the isolated annual-news relevance annotation lab."""
from __future__ import annotations
import argparse
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'agent'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--package-dir',type=Path,default=ROOT/'var/news-events-annual-20261027/retrieval-lab-v1')
    p.add_argument('--model-dir',type=Path,default=ROOT/'var/knowledge-snapshots/rag-recall-demo/data/models/bge-large-zh-v1.5')
    p.add_argument('--database',type=Path)
    p.add_argument('--device',choices=['cpu','cuda'],default='cuda')
    p.add_argument('--embedding-python',type=Path)
    p.add_argument('--port',type=int,default=8877)
    p.add_argument('--build-only',action='store_true')
    args=p.parse_args()
    if not 1024<=args.port<=65535:
        p.error('port must be within 1024..65535')
    from app.modules.retrieval_lab.engine import RetrievalLabEngine
    from app.modules.retrieval_lab.store import RetrievalLabStore
    from app.modules.retrieval_lab.api import LabService,create_app
    import uvicorn
    engine=RetrievalLabEngine(args.package_dir,args.model_dir,device=args.device,
                             embedding_python=args.embedding_python)
    try:
        print(engine.build(batch_size=16),flush=True)
        store=RetrievalLabStore(args.database or args.package_dir/'judgments.sqlite3')
        service=LabService(args.package_dir,engine,store)
        service.seed()
        if args.build_only:
            print(service.status(),flush=True)
            return
        uvicorn.run(create_app(service),host='127.0.0.1',port=args.port,log_level='info')
    finally:
        engine.close()

if __name__=='__main__':
    main()
