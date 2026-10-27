"""Persistent, isolated annual-corpus semantic retrieval for human annotation.

This lab uses real local BGE vectors and exact cosine search. It does not mutate
production lifecycle stores, invent publication times, or infer gold labels.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import threading
from time import perf_counter

import numpy as np

from app.retrieval.bge_snapshot import BGE_QUERY_INSTRUCTION
from app.retrieval.query_policy import QueryRetrievalPolicy, rank_candidates
from app.retrieval.tiered_vector import VectorSearchHit, VectorTier

DIMENSIONS = 1024
CHUNK_CHARS = 480
CHUNK_OVERLAP = 60
TITLE_CHARS = 24
STRATEGY_VERSION = 'annual-lab-global-cosine-v1'
CHUNK_POLICY = 'title24-body480-overlap60-tokencheck512-v1'


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_bytes(_json_bytes(value))
    temporary.replace(path)


def _checked_vectors(value, count):
    result = np.asarray(value, dtype=np.float32)
    if result.shape != (count, DIMENSIONS) or not np.isfinite(result).all():
        raise ValueError('embedding count, dimension or finite-coordinate check failed')
    if not np.allclose(np.linalg.norm(result, axis=1), 1.0, atol=0.001):
        raise ValueError('BGE vectors must be L2 normalized')
    return result


def chunk_news(news):
    """Every body character remains covered; tokenization may never truncate."""
    chunks = []
    for article in news:
        body = article['body']
        for number, start in enumerate(range(0, len(body), CHUNK_CHARS - CHUNK_OVERLAP)):
            excerpt = body[start:start + CHUNK_CHARS]
            chunks.append({'chunk_id': article['news_id'] + ':' + str(number),
                           'news_id': article['news_id'], 'tier': article['tier'],
                           'start': start, 'end': start + len(excerpt), 'excerpt': excerpt,
                           'text': article['title'][:TITLE_CHARS] + '\n' + excerpt})
            if start + CHUNK_CHARS >= len(body):
                break
    return chunks


class OfflineBgeProcess:
    semantic_quality_ready = True

    def __init__(self, *, python, model_dir, device, log_path):
        self.python, self.model_dir, self.device = str(python), str(model_dir), device
        self.log_path = Path(log_path)
        self._process = None
        self._lock = threading.Lock()
        self._log = None
        self.runtime_info = {}

    def _read(self):
        selector = selectors.DefaultSelector()
        selector.register(self._process.stdout, selectors.EVENT_READ)
        try:
            if not selector.select(timeout=180):
                raise TimeoutError('local BGE encoder exceeded 180 second request budget')
            line = self._process.stdout.readline(16 * 1024 * 1024)
            if not line or len(line) >= 16 * 1024 * 1024:
                raise RuntimeError('local BGE encoder stopped or exceeded response budget')
            result = json.loads(line)
            if 'error' in result:
                raise ValueError('local BGE encoder: ' + result.get('message', result['error']))
            return result
        finally:
            selector.close()

    def _start(self):
        if self._process is not None:
            if self._process.poll() is not None:
                raise RuntimeError('local BGE encoder has stopped')
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = self.log_path.open('ab')
        allowed = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'LD_LIBRARY_PATH', 'CUDA_VISIBLE_DEVICES')
        environ = {key: os.environ[key] for key in allowed if key in os.environ}
        environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false')
        self._process = subprocess.Popen(
            [self.python, '-u', str(Path(__file__).with_name('embedding_worker.py')),
             '--model-dir', self.model_dir, '--device', self.device],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log,
            text=True, encoding='utf-8', env=environ, bufsize=1)
        self.runtime_info = self._read()
        if not self.runtime_info.get('ready') or self.runtime_info.get('dimensions') != DIMENSIONS:
            raise ValueError('local BGE encoder startup contract failed')

    def encode(self, texts):
        with self._lock:
            try:
                self._start()
                self._process.stdin.write(json.dumps({'texts': texts}, ensure_ascii=False) + '\n')
                self._process.stdin.flush()
                result = self._read()
                if result.get('truncated_inputs') != 0:
                    raise ValueError('silent model truncation is forbidden')
                counts = result.get('token_counts', [])
                if len(counts) != len(texts) or any(type(n) is not int or not 1 <= n <= 512 for n in counts):
                    raise ValueError('model token count validation failed')
                return _checked_vectors(result['vectors'], len(texts)), counts
            except BaseException:
                # A timed-out response must never be consumed by the next query.
                self._shutdown()
                raise

    def _shutdown(self):
        if self._process is not None:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=10)
            self._process = None
        if self._log is not None:
            self._log.close()
            self._log = None

    def close(self):
        with self._lock:
            self._shutdown()


class RetrievalLabEngine:
    def __init__(self, package_dir, model_dir, device='cpu', *, embedding_python=None, encoder=None):
        self.package_dir, self.model_dir = Path(package_dir).resolve(), Path(model_dir).resolve()
        self.manifest = json.loads((self.package_dir / 'manifest.json').read_text('utf-8'))
        self.news_path = self.package_dir / 'news.jsonl'
        self.corpus_sha = _sha(self.news_path)
        file_info = self.manifest.get('files', {}).get('news.jsonl', {})
        declared = file_info.get('sha256')
        if not isinstance(declared, str) or declared != self.corpus_sha:
            raise ValueError('corpus news SHA-256 missing or mismatch')
        if not isinstance(self.manifest.get('corpus_version'), str) or not self.manifest['corpus_version']:
            raise ValueError('corpus_version is required')
        self.as_of_date = date.fromisoformat(self.manifest['as_of_date'])
        self.news = [json.loads(line) for line in self.news_path.read_text('utf-8').splitlines() if line.strip()]
        if not self.news or len(self.news) > 10000:
            raise ValueError('lab requires 1..10000 canonical articles')
        if type(file_info.get('rows')) is not int or file_info['rows'] != len(self.news):
            raise ValueError('corpus news row count mismatch')
        self.by_id = {}
        for article in self.news:
            identifier = article.get('news_id')
            if not isinstance(identifier, str) or not identifier or identifier in self.by_id:
                raise ValueError('canonical news IDs must be unique and nonblank')
            if not article.get('body') or not article.get('title') or article.get('tier') not in {t.value for t in VectorTier}:
                raise ValueError('news body, title and hot/warm/cold tier are required')
            if date.fromisoformat(article['publication_date']) > self.as_of_date:
                raise ValueError('future article exceeds frozen corpus date')
            if article.get('publish_time'):
                actual = datetime.fromisoformat(article['publish_time'].replace('Z', '+00:00'))
                if actual.tzinfo is None:
                    raise ValueError('precise publication times require an explicit timezone')
            self.by_id[identifier] = article
        self.news.sort(key=lambda item: item['news_id'])
        self.chunks = chunk_news(self.news)
        if len(self.chunks) > 100000:
            raise ValueError('lab chunk count exceeds budget')
        self.index_dir = self.package_dir / 'vector-index'
        self.index_dir.mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self.vectors = None
        self.index_manifest = None
        self.policy = QueryRetrievalPolicy(version=STRATEGY_VERSION, min_per_tier=20,
            topic_rules={category: (category,) for category in sorted({
                category for article in self.news for category in article.get('categories', [])})})
        self.model_files = self._model_identity() if encoder is None else {'test-double': 'injected'}
        self.model_version = 'bge-large-zh-v1.5:' + hashlib.sha256(_json_bytes(self.model_files)).hexdigest()[:20]
        if encoder is None:
            if embedding_python is None:
                root = Path(__file__).resolve().parents[4]
                embedding_python = root / 'var/news-events-annual-20261027/runtime/embedding-venv/bin/python'
            if not Path(embedding_python).is_file():
                raise ValueError('isolated BGE Python executable is unavailable')
            encoder = OfflineBgeProcess(python=embedding_python, model_dir=self.model_dir,
                                       device=device, log_path=self.index_dir / 'encoder.log')
        self.encoder = encoder
        self.versions = {'corpus': self.manifest['corpus_version'], 'model': self.model_version,
                         'strategy': STRATEGY_VERSION}
        self.semantic_quality_ready = bool(getattr(self.encoder, 'semantic_quality_ready', False))

    def _model_identity(self):
        required = ('config.json', 'tokenizer.json', 'tokenizer_config.json', 'vocab.txt',
                    'pytorch_model.bin', 'modules.json', '1_Pooling/config.json')
        if any(not (self.model_dir / name).is_file() for name in required):
            raise ValueError('local BGE model assets are incomplete')
        config = json.loads((self.model_dir / 'config.json').read_text('utf-8'))
        pooling = json.loads((self.model_dir / '1_Pooling/config.json').read_text('utf-8'))
        if (config.get('hidden_size') != DIMENSIONS or config.get('model_type') != 'bert'
                or pooling.get('pooling_mode_cls_token') is not True
                or any(pooling.get(name) for name in ('pooling_mode_mean_tokens', 'pooling_mode_max_tokens',
                                                     'pooling_mode_mean_sqrt_len_tokens'))):
            raise ValueError('local model must match BGE CLS pooling contract')
        assets = [path for path in sorted(self.model_dir.rglob('*')) if path.is_file()]
        if any(not path.resolve().is_relative_to(self.model_dir) for path in assets):
            raise ValueError('model asset symlink escapes model directory')
        return {str(path.relative_to(self.model_dir)): _sha(path) for path in assets}

    def _identity(self):
        return {'corpus_sha256': self.corpus_sha, 'model_version': self.model_version,
                'chunk_policy': CHUNK_POLICY, 'chunk_count': len(self.chunks),
                'chunks_sha256': hashlib.sha256(_json_bytes(self.chunks)).hexdigest(),
                'semantic_quality_ready': self.semantic_quality_ready}

    def build(self, batch_size=16):
        if type(batch_size) is not int or not 1 <= batch_size <= 64:
            raise ValueError('batch_size must be 1..64')
        with self._lock:
            begun = perf_counter()
            manifest_path = self.index_dir / 'manifest.json'
            vector_path = self.index_dir / 'vectors.npy'
            if manifest_path.exists():
                existing = json.loads(manifest_path.read_text('utf-8'))
                if existing.get('identity') != self._identity() or _sha(vector_path) != existing.get('vectors_sha256'):
                    raise ValueError('frozen vector index integrity or identity mismatch')
                self.vectors = _checked_vectors(np.load(vector_path, allow_pickle=False), len(self.chunks))
                self.index_manifest = existing
                return {**existing, 'resumed': True, 'elapsed_ms': (perf_counter() - begun) * 1000}
            batches = self.index_dir / 'batches'
            batches.mkdir(exist_ok=True)
            values, token_counts, reused = [], [], 0
            for start in range(0, len(self.chunks), batch_size):
                texts = [item['text'] for item in self.chunks[start:start + batch_size]]
                batch_id = hashlib.sha256(_json_bytes({'texts': texts, 'model': self.model_version})).hexdigest()
                batch_file, info_file = batches / (batch_id + '.npy'), batches / (batch_id + '.json')
                if info_file.exists():
                    info = json.loads(info_file.read_text('utf-8'))
                    if info.get('input_sha256') != batch_id or _sha(batch_file) != info.get('vectors_sha256'):
                        raise ValueError('cached embedding batch checksum mismatch')
                    vectors = _checked_vectors(np.load(batch_file, allow_pickle=False), len(texts))
                    counts = info['token_counts']
                    reused += 1
                else:
                    vectors, counts = self.encoder.encode(texts)
                    vectors = _checked_vectors(vectors, len(texts))
                    temp = batch_file.with_suffix('.npy.tmp')
                    with temp.open('wb') as stream:
                        np.save(stream, vectors, allow_pickle=False)
                    temp.replace(batch_file)
                    _atomic_json(info_file, {'input_sha256': batch_id, 'vectors_sha256': _sha(batch_file),
                                            'token_counts': counts, 'truncated_inputs': 0})
                if len(counts) != len(texts) or any(type(n) is not int or not 1 <= n <= 512 for n in counts):
                    raise ValueError('cached token budget validation failed')
                values.append(vectors)
                token_counts.extend(counts)
                _atomic_json(self.index_dir / 'progress.json', {'encoded_chunks': sum(len(v) for v in values),
                    'total_chunks': len(self.chunks), 'cached_batches': reused, 'status': 'building'})
            self.vectors = _checked_vectors(np.concatenate(values), len(self.chunks))
            temp = vector_path.with_suffix('.npy.tmp')
            with temp.open('wb') as stream:
                np.save(stream, self.vectors, allow_pickle=False)
            temp.replace(vector_path)
            result = {'schema_version': 1, 'identity': self._identity(), 'versions': self.versions,
                      'vectors_sha256': _sha(vector_path), 'model_files': self.model_files,
                      'dimensions': DIMENSIONS, 'metric': 'cosine', 'backend': 'numpy_exact',
                      'news_count': len(self.news), 'chunk_count': len(self.chunks),
                      'max_actual_tokens': max(token_counts), 'truncated_inputs': 0,
                      'tier_counts': dict(Counter(article['tier'] for article in self.news)),
                      'encoder_runtime': getattr(self.encoder, 'runtime_info', {}),
                      'cached_batches': reused, 'elapsed_ms': (perf_counter() - begun) * 1000}
            _atomic_json(manifest_path, result)
            _atomic_json(self.index_dir / 'progress.json', {'status': 'complete', 'encoded_chunks': len(self.chunks),
                                                          'total_chunks': len(self.chunks)})
            self.index_manifest = result
            return result

    def search(self, query, limit=10, category=None):
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise ValueError('query must contain 1..4000 characters')
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('limit must be 1..50')
        if self.vectors is None:
            raise RuntimeError('build or load the frozen vector index before searching')
        if category is not None and category not in self.policy.topic_rules:
            raise ValueError('unknown category hint')
        begun = perf_counter()
        with self._lock:
            plan = self.policy.plan(query, limit=limit, default_candidates=20,
                                    max_candidates=200, available_tiers=tuple(VectorTier), topic=category)
            encoded_at = perf_counter()
            query_vectors, counts = self.encoder.encode([BGE_QUERY_INSTRUCTION + query.strip()])
            vector = _checked_vectors(query_vectors, 1)[0]
            embedding_ms = (perf_counter() - encoded_at) * 1000
            search_at = perf_counter()
            similarities = self.vectors @ vector
            all_hits, tiers = [], {}
            for tier in VectorTier:
                tier_at = perf_counter()
                positions = [number for number, item in enumerate(self.chunks) if item['tier'] == tier.value]
                positions.sort(key=lambda number: (-float(similarities[number]), self.chunks[number]['chunk_id']))
                picked, seen, examined = [], set(), 0
                for number in positions:
                    examined += 1
                    chunk = self.chunks[number]
                    if chunk['news_id'] in seen:
                        continue
                    seen.add(chunk['news_id'])
                    article = self.by_id[chunk['news_id']]
                    published = (datetime.fromisoformat(article['publish_time'].replace('Z', '+00:00'))
                                 if article.get('publish_time') else None)
                    picked.append(VectorSearchHit(chunk['chunk_id'], chunk['news_id'], article['title'],
                        chunk['excerpt'], article.get('url') or article.get('source_url'), published,
                        float(similarities[number]), tier, 1, self.model_version))
                    if len(picked) >= plan.tier_limits[tier]:
                        break
                all_hits.extend(picked)
                tiers[tier.value] = {'candidate_limit': plan.tier_limits[tier], 'available_chunks': len(positions),
                    'available_news': sum(article['tier'] == tier.value for article in self.news),
                    'examined_chunks': examined, 'duplicate_chunks_skipped': examined - len(picked),
                    'returned_news': len(picked), 'exhausted': examined == len(positions),
                    'elapsed_ms': (perf_counter() - tier_at) * 1000}
            search_ms = (perf_counter() - search_at) * 1000
            fusion_at = perf_counter()
            ranked, _, ranking = rank_candidates(all_hits, embedding_version=self.model_version,
                                                  rrf_k=60, limit=limit)
            candidates = [{**self.by_id[hit.news_id], 'rank': rank, 'score': hit.raw_score,
                           'score_kind': 'cosine_similarity', 'matched_excerpt': hit.excerpt,
                           'matched_chunk_id': hit.chunk_id} for rank, hit in enumerate(ranked, start=1)]
            fusion_ms = (perf_counter() - fusion_at) * 1000
            trace = {'schema_version': 1, 'status': 'complete', 'backend': 'numpy_exact',
                'semantic_quality_ready': self.semantic_quality_ready,
                'as_of_date': self.as_of_date.isoformat(), 'publication_precision_preserved': True,
                'tier_basis': self.manifest.get('tier_policy', 'publication_date_age'),
                'retrieval_plan': {'intent': plan.intent, 'topic': plan.topic, 'reason': plan.reason,
                    'category_mode': 'soft_hint_no_filter', 'fusion': 'global_similarity'},
                'stages': [{'name': 'embedding', 'elapsed_ms': embedding_ms, 'query_tokens': counts[0]},
                           {'name': 'tier_search_and_news_dedup', 'elapsed_ms': search_ms,
                            'input_chunks': len(self.chunks), 'output_news': len(all_hits)},
                           {'name': 'global_cosine_fusion', 'elapsed_ms': fusion_ms,
                            'input_news': len(all_hits), 'output_news': len(candidates)}],
                'tiers': tiers, 'ranking': ranking, 'total_ms': (perf_counter() - begun) * 1000,
                'relevance_labels': 'human_only', 'similarity_is_accuracy': False,
                'temporal_scope': 'frozen_date_snapshot_not_historical_intraday_replay'}
            return {'candidates': candidates, 'trace': trace, 'versions': self.versions}

    def close(self):
        self.encoder.close()
