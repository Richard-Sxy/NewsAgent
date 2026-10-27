"""Contract tests use an explicit test encoder, never model-quality claims."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from app.modules.retrieval_lab.engine import RetrievalLabEngine, chunk_news, DIMENSIONS


class FixtureEncoder:
    semantic_quality_ready = False
    def __init__(self):
        self.calls = []
    def encode(self, texts):
        self.calls.append(list(texts))
        vectors = []
        for text in texts:
            vector = np.zeros(DIMENSIONS, dtype=np.float32)
            vector[0] = 1 if '相关' in text else .7
            vector[1] = 0 if '相关' in text else (1 - .7 ** 2) ** .5
            vectors.append(vector)
        return np.asarray(vectors), [min(510, len(text) + 2) for text in texts]
    def close(self):
        pass


class EngineContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.records = [
            {'news_id': 'a', 'title': '相关人工智能', 'body': '相关资料。' * 1000, 'tier': 'hot',
             'publication_date': '2026-10-26', 'publish_time': None, 'categories': ['AI'],
             'url': 'https://example.com/a', 'duplicate_sources': ['a-original']},
            {'news_id': 'b', 'title': '其他科技', 'body': '这是其他新闻的完整正文。' * 30, 'tier': 'hot',
             'publication_date': '2026-10-26', 'publish_time': None, 'categories': ['科技'],
             'url': 'https://example.com/b'},
            {'news_id': 'c', 'title': '相关财经', 'body': '相关资料。' * 50, 'tier': 'cold',
             'publication_date': '2026-01-01', 'publish_time': None, 'categories': ['财经'],
             'url': 'https://example.com/c'},
        ]
        self._save()
        self.encoder = FixtureEncoder()

    def tearDown(self):
        self.temp.cleanup()

    def _save(self):
        raw = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in self.records).encode()
        (self.root / 'news.jsonl').write_bytes(raw)
        (self.root / 'manifest.json').write_text(json.dumps({'corpus_version': 'fixture-v1',
            'as_of_date': '2026-10-27', 'files': {'news.jsonl': {'sha256': hashlib.sha256(raw).hexdigest(), 'rows': len(self.records)}},
            'tier_policy': 'date-age-7-90-v1'}))

    def engine(self):
        return RetrievalLabEngine(self.root, self.root / 'unused-model', encoder=self.encoder)

    def test_chunk_coverage_and_input_budget(self):
        chunks = chunk_news(self.records)
        for record in self.records:
            parts = [c for c in chunks if c['news_id'] == record['news_id']]
            covered = set()
            for part in parts:
                self.assertEqual(part['excerpt'], record['body'][part['start']:part['end']])
                self.assertLessEqual(len(part['text']), 505)
                covered.update(range(part['start'], part['end']))
            self.assertEqual(covered, set(range(len(record['body']))))

    def test_build_resume_and_model_double_explicit(self):
        engine = self.engine()
        first = engine.build(batch_size=4)
        self.assertFalse(first['identity']['semantic_quality_ready'])
        self.assertEqual(first['dimensions'], 1024)
        old_calls = len(self.encoder.calls)
        second = self.engine().build(batch_size=4)
        self.assertTrue(second['resumed'])
        self.assertEqual(len(self.encoder.calls), old_calls)

    def test_search_refills_unique_news_and_preserves_dates(self):
        engine = self.engine()
        engine.build()
        result = engine.search('相关背景', limit=3)
        rows = result['candidates']
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r['news_id'] for r in rows}), 3)
        self.assertIsNone(rows[0]['publish_time'])
        self.assertEqual(rows[0]['duplicate_sources'], ['a-original'])
        self.assertGreater(result['trace']['tiers']['hot']['duplicate_chunks_skipped'], 0)
        self.assertFalse(result['trace']['similarity_is_accuracy'])
        self.assertNotIn('relevance_label', rows[0])
        self.assertTrue(self.encoder.calls[-1][0].startswith('为这个句子生成表示'))

    def test_category_hint_does_not_filter(self):
        engine = self.engine()
        engine.build()
        result = engine.search('相关', limit=3, category='AI')
        self.assertEqual(len(result['candidates']), 3)
        self.assertEqual(result['trace']['retrieval_plan']['category_mode'], 'soft_hint_no_filter')

    def test_vector_tampering_rejected(self):
        engine = self.engine()
        engine.build()
        path = self.root / 'vector-index/vectors.npy'
        with path.open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaisesRegex(ValueError, 'integrity'):
            self.engine().build()

    def test_batch_tampering_rejected(self):
        engine = self.engine()
        engine.build(batch_size=4)
        (self.root / 'vector-index/manifest.json').unlink()
        path = next((self.root / 'vector-index/batches').glob('*.npy'))
        with path.open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.engine().build(batch_size=4)

    def test_corpus_tampering_rejected(self):
        with (self.root / 'news.jsonl').open('ab') as stream:
            stream.write(b' ')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            self.engine()

    def test_future_and_naive_date_rejected(self):
        self.records[0]['publication_date'] = '2026-10-28'
        self._save()
        with self.assertRaisesRegex(ValueError, 'future'):
            self.engine()
        self.records[0]['publication_date'] = '2026-10-26'
        self.records[0]['publish_time'] = '2026-10-26T12:00:00'
        self._save()
        with self.assertRaisesRegex(ValueError, 'timezone'):
            self.engine()

    def test_duplicate_ids_rejected(self):
        self.records[1]['news_id'] = 'a'
        self._save()
        with self.assertRaisesRegex(ValueError, 'unique'):
            self.engine()

    def test_unready_and_query_budgets_rejected(self):
        engine = self.engine()
        with self.assertRaisesRegex(RuntimeError, 'before searching'):
            engine.search('新闻')
        engine.build()
        for kwargs in ({'query': ''}, {'query': 'a', 'limit': 0}, {'query': 'a', 'limit': True},
                       {'query': 'a', 'category': 'unknown'}):
            with self.assertRaises(ValueError):
                engine.search(**kwargs)

    def test_unnormalized_vectors_rejected(self):
        self.encoder.encode = lambda texts: (np.ones((len(texts), 1024)), [10] * len(texts))
        with self.assertRaisesRegex(ValueError, 'normalized'):
            self.engine().build()

    def test_token_overflow_rejected(self):
        real_encode = self.encoder.encode
        self.encoder.encode = lambda texts: (real_encode(texts)[0], [513] * len(texts))
        with self.assertRaisesRegex(ValueError, 'token'):
            self.engine().build()


if __name__ == '__main__':
    unittest.main()
