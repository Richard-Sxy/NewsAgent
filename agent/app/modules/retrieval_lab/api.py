"""Local retrieval review HTTP application; no production Settings or credentials."""
from __future__ import annotations
from hashlib import sha256
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .store import RetrievalLabStore, RevisionConflict, FrozenRunConflict


class SearchInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=512)
    category: str | None = Field(default=None,max_length=40)
    limit: int = Field(default=10,strict=True,ge=1,le=50)


class JudgmentInput(BaseModel):
    model_config = ConfigDict(extra='forbid',str_strip_whitespace=True)
    label: str
    notes: str = Field(default='',max_length=10000)
    reviewer: str = Field(default='local-human',min_length=1,max_length=120)
    expected_revision: int = Field(strict=True,ge=0)


class LabService:
    def __init__(self, package_dir, engine, store):
        self.package_dir=Path(package_dir)
        self.engine,self.store=engine,store
        self.manifest=json.loads((self.package_dir/'manifest.json').read_text(encoding='utf-8'))
        raw=(self.package_dir/'queries.jsonl').read_bytes()
        expected=self.manifest['files']['queries.jsonl']
        if sha256(raw).hexdigest()!=expected['sha256']:
            raise ValueError('query tasks failed frozen corpus integrity check')
        self.presets=[json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
        if len(self.presets)!=expected['rows']:
            raise ValueError('query count differs from frozen manifest')
        self.versions = dict(engine.versions)
        if self.versions.get('corpus') != self.manifest['corpus_version']:
            raise ValueError('engine corpus version differs from frozen manifest')
        if any(r['versions'] != self.versions for r in self.store.list_runs()):
            raise ValueError('model or strategy changed; use a new experiment database')
        self.store.set_manifest(self.manifest)
        self.search_lock=threading.Lock()

    def search(self, query, *, category=None, limit=10, query_id=None):
        if not self.search_lock.acquire(blocking=False):
            raise HTTPException(409,'另一个查询正在检索，请稍后重试。')
        try:
            if len(self.store.list_runs()) >= 1000:
                raise ValueError('当前实验达到1000个冻结查询上限，请另建实验版本。')
            result=self.engine.search(query,limit=limit,category=category)
            if result.get('versions') != self.versions:
                raise ValueError('retrieval version drift; result was not saved')
            run_id='run-'+uuid4().hex
            run={**result,'run_id':run_id,'query_id':query_id or 'custom-'+uuid4().hex,
                 'query':query,'category':category,'requested_limit':limit,
                 'as_of_date':self.manifest['as_of_date']}
            return self.store.put_run(run)
        finally:
            self.search_lock.release()

    def seed(self):
        existing={r['query_id'] for r in self.store.list_runs()}
        for preset in self.presets:
            if preset['query_id'] in existing:
                continue
            result=self.search(preset['text'],category=preset['category'],
                limit=preset.get('limit',10),query_id=preset['query_id'])
            print(json.dumps({'phase':'query_preparation','query_id':preset['query_id'],
                'category':preset['category'],'candidates':len(result['candidates'])},ensure_ascii=False),flush=True)

    def status(self):
        m=self.manifest
        runs=self.store.list_runs()
        versions=self.versions
        return {'title':'年度新闻召回 · 人工相关性标注',
            'corpus':{'document_count':m['source_count'],'canonical_count':m['canonical_count'],
                'version':m['corpus_version'],'as_of_date':m['as_of_date'],
                'tier_counts':m['tier_counts'],'category_counts':m['category_counts']},
            'model':{'name':'BGE-large-zh-v1.5（本地真实语义模型）','version':versions.get('model','building')},
            'strategy':{'name':'三层候选召回＋全局余弦排序＋新闻去重',
                'version':versions.get('strategy','building'),'tier_assignment':m['tier_policy']},
            'query_count':len(runs),'preset_query_count':len(self.presets),
            'judgment_count':sum(r['progress']['reviewed'] for r in runs),
            'warnings':m['warnings']+['当前为单人标注实验；池内指标不能代表完整语料的召回率。'],
            'persistence':'server_sqlite','ready':{p['query_id'] for p in self.presets} <= {r['query_id'] for r in runs}}


def create_app(service: LabService):
    app=FastAPI(title='Annual News Retrieval Review',docs_url=None,redoc_url=None,openapi_url=None)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['127.0.0.1','localhost'])

    @app.middleware('http')
    async def local_boundaries(request:Request,call_next):
        if request.method in ('POST','PUT','DELETE','PATCH'):
            origin=request.headers.get('origin')
            try:
                origin_allowed = not origin or urlsplit(origin).netloc == request.headers.get('host')
            except ValueError:
                origin_allowed = False
            if request.headers.get('x-lab-request')!='1' or not origin_allowed:
                return JSONResponse({'detail':'标注操作仅允许来自当前本地页面。'},status_code=403)
            if request.headers.get('content-type','').split(';')[0]!='application/json':
                return JSONResponse({'detail':'JSON body required'},status_code=415)
            length=request.headers.get('content-length')
            if length is not None and (not length.isdigit() or int(length)>65536):
                return JSONResponse({'detail':'request too large'},status_code=413)
            # Also cap chunked bodies before JSON decoding, irrespective of Content-Length.
            chunks=[]
            size=0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 65536:
                    return JSONResponse({'detail':'request too large'},status_code=413)
                chunks.append(chunk)
            request._body = b''.join(chunks)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.exception_handler(RevisionConflict)
    async def conflict(request,exc):
        return JSONResponse({'detail':'标注已在其他页面更新，请重新加载。','current':exc.current},status_code=409)

    @app.exception_handler(FrozenRunConflict)
    async def frozen_conflict(request,exc):
        return JSONResponse({'detail':str(exc)},status_code=409)

    @app.exception_handler(KeyError)
    async def missing(request,exc):
        return JSONResponse({'detail':'查询或新闻不存在。'},status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request,exc):
        return JSONResponse({'detail':str(exc)},status_code=422)

    @app.get('/api/status')
    def status():
        return service.status()

    @app.get('/api/queries')
    def queries():
        return {'items':service.store.list_runs()}

    @app.get('/api/runs/{run_id}')
    def run(run_id:str):
        return service.store.get_run(run_id)

    @app.post('/api/search')
    def search(body:SearchInput):
        return service.search(body.query,category=body.category,limit=body.limit)

    @app.put('/api/runs/{run_id}/judgments/{news_id}')
    def judgment(run_id:str,news_id:str,body:JudgmentInput):
        return service.store.save_judgment(run_id,news_id,**body.model_dump())

    @app.get('/api/export')
    def export():
        return JSONResponse(service.store.export_data(),headers={
            'Content-Disposition':'attachment; filename="annual-news-human-judgments.json"'})

    static=Path(__file__).parent/'static'
    app.mount('/static',StaticFiles(directory=static),name='static')

    @app.get('/')
    def home():
        return FileResponse(static/'index.html')

    return app
