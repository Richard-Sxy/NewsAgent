"""Immutable aggregate snapshot -> existing NewsAgent metric/ranking/analysis ports."""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from hashlib import sha256
import json
import re

from app.analytics.entities import ContentType
from app.analytics.news_content import NewsContent, InMemoryNewsContentRepository
from app.modules.hot_news.scoring.metrics import NewsMetricSnapshot
from app.modules.hot_news.scoring.metric_source import HotNewsMetricQuery
from app.modules.hot_news.application.hot_news_orchestration import (
    HotNewsRunRequest, HotNewsOrchestrationPolicy, HotNewsOrchestrationService,
    EmptyHotNewsBaselineProvider,
)
from app.modules.hot_news.analysis.hot_news_enrichment import HotNewsEnrichmentService
from app.modules.hot_news.analysis.hot_news_analysis import HotNewsAnalysisService, HotNewsAnalysisInputBuilder, HotNewsAnalysisValidator
from app.modules.hot_news.analysis.hot_news_analysis_batch import HotNewsAnalysisBatch
from app.modules.hot_news.analysis.runner import NativeHotNewsAnalysisRunner, local_hot_news_prompts
from app.model_runtime.core import StructuredInferenceService
from app.model_runtime.local_inference import LocalHotNewsInference

TENANTS = {'11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222'}
BUNDLE = 'streaming-lab-bundle-v1'
CATALOG = {
    'synthetic-article-1': ('article', '合成实验：城市公共交通服务调整'),
    'synthetic-video-2': ('video', '合成实验：科学展览视频导览'),
    'synthetic-article-3': ('article', '合成实验：社区阅读活动开放报名'),
}
FIELDS = ('impressions','clicks','unique_users','total_duration_seconds','effective_consumptions','interactions')


def utc(milliseconds):
    return datetime.fromtimestamp(milliseconds/1000, timezone.utc)


def validate_payload(payload_text: str, snapshot_id: str, tenant_id: str, *, catalog=None) -> dict:
    if not re.fullmatch('[0-9a-f]{64}', snapshot_id) or sha256(payload_text.encode()).hexdigest() != snapshot_id:
        raise ValueError('Snapshot hash mismatch')
    body = json.loads(payload_text)
    if tenant_id not in TENANTS or body['tenant_id'] != tenant_id:
        raise ValueError('Snapshot tenant mismatch')
    from simulation.streaming_lab.window_query import VERSION as WINDOW_VERSION, validate_composition
    composed = body['metric_version'] == WINDOW_VERSION
    public = body['metric_version'] in ('synthetic-public-news-metrics-v1', WINDOW_VERSION)
    if public:
        if (catalog is None or body.get('catalog_id') != catalog['catalog_id']
                or tenant_id not in catalog['manifest']['tenants']):
            raise ValueError('Unregistered public content catalog/tenant')
    elif body['metric_version'] != 'synthetic-event-metrics-v1':
        raise ValueError('Unregistered metric version')
    if composed:
        validate_composition(body)
    width = body['window_end_ms']-body['window_start_ms']
    if (body['window_start_ms'] % 60000 or (not composed and width != 60000)
            or type(body['final']) is not bool or type(body['event_count']) is not int
            or not 0 < body['event_count'] <= 10000 * (width // 60000)):
        raise ValueError('Invalid bounded window')
    if not re.fullmatch('[a-z0-9-]{1,64}', body['run_id']) or not re.fullmatch('[0-9a-f]{64}', body['event_digest']):
        raise ValueError('Invalid snapshot identity')
    seen = set()
    for row in body['metrics']:
        news_id = row['news_id']
        expected = catalog['news'].get(news_id) if public else None
        if (news_id in seen or (public and (not expected or row['content_type'] != expected['content_type']
                or expected['publish_ms'] > (body['window_end_ms']-60000 if composed else body['window_start_ms'])))
                or (not public and (news_id not in CATALOG or row['content_type'] != CATALOG[news_id][0]))):
            raise ValueError('Unknown or duplicate news identity')
        seen.add(news_id)
        if any(type(row[k]) is not int or row[k] < 0 for k in FIELDS):
            raise ValueError('Invalid metric counter')
        if not row['unique_users'] <= row['clicks'] <= row['impressions'] or row['effective_consumptions'] > row['clicks']:
            raise ValueError('Invalid synthetic funnel')
    if not seen:
        raise ValueError('Empty snapshot')
    return body


@dataclass(frozen=True, slots=True)
class SnapshotRunRequest(HotNewsRunRequest):
    """Lab-only revision identity; legacy requests and production bundle semantics stay intact."""
    snapshot_sha256: str = ''
    retrieval_context_sha256: str | None = None

    def validate(self):
        HotNewsRunRequest.validate(self)
        if not re.fullmatch('[0-9a-f]{64}', self.snapshot_sha256):
            raise ValueError('Snapshot identity is required')
        if self.retrieval_context_sha256 is not None and not re.fullmatch('[0-9a-f]{64}', self.retrieval_context_sha256):
            raise ValueError('Invalid retrieval context identity')

    @property
    def idempotency_key(self):
        self.validate()
        legacy = HotNewsRunRequest.idempotency_key.fget(self)
        identity = legacy+'\x1f'+self.snapshot_sha256
        if self.retrieval_context_sha256 is not None:
            identity += '\x1f'+self.retrieval_context_sha256
        return 'hot-news-stream-' + sha256(identity.encode()).hexdigest()


class FrozenSnapshotMetricSource:
    def __init__(self, payload_text, snapshot_id, tenant_id, *, catalog=None):
        self.body = validate_payload(payload_text, snapshot_id, tenant_id, catalog=catalog)

    async def fetch_snapshots(self, query: HotNewsMetricQuery):
        query.validate()
        body = self.body
        if (query.tenant_id != body['tenant_id'] or query.window_start != utc(body['window_start_ms'])
                or query.window_end != utc(body['window_end_ms'])):
            raise ValueError('Query crossed frozen snapshot identity')
        return [NewsMetricSnapshot(
            news_id=r['news_id'], content_type=ContentType(r['content_type']),
            window_start=query.window_start, window_end=query.window_end,
            **{k:r[k] for k in FIELDS},
            ctr=Decimal(r['clicks'])/Decimal(r['impressions']) if r['impressions'] else Decimal(0),
        ) for r in body['metrics'] if not query.content_types or ContentType(r['content_type']) in query.content_types]


class ExplicitEmptyRelatedNews:
    async def batch_search_related_news(self, queries, *, tenant_id):
        if tenant_id not in TENANTS:
            raise ValueError('Foreign synthetic tenant')
        return {q.query_id: [] for q in queries}


async def analyze_snapshot(payload_text, snapshot_id, tenant_id, *, catalog=None, content_repository=None, reader_service=None, related_search=None):
    source = FrozenSnapshotMetricSource(payload_text, snapshot_id, tenant_id, catalog=catalog)
    body = source.body
    from simulation.streaming_lab.window_query import VERSION as WINDOW_VERSION, WindowBaselineProvider
    composed = body['metric_version'] == WINDOW_VERSION
    public = body['metric_version'] in ('synthetic-public-news-metrics-v1', WINDOW_VERSION)
    if public and content_repository is None:
        raise ValueError('Public news requires the versioned SQL content repository')
    bundle = 'streaming-public-news-bundle-v1' if public else BUNDLE
    prompt_version='native-e2e-prompt-v3-reader-profiles' if reader_service else 'native-e2e-prompt-v1'
    if reader_service:
        bundle='streaming-public-readers-bundle-v1'
    if related_search is not None:
        trace = related_search.audit()
        if (not public or trace['tenant_id'] != tenant_id or trace['catalog_id'] != catalog['catalog_id']
                or trace['as_of_ms'] != body['window_end_ms']):
            raise ValueError('Retrieval context differs from snapshot scope')
        bundle = 'streaming-public-readers-retrieval-v2' if reader_service else 'streaming-public-retrieval-v2'
    if composed:
        if reader_service is None:
            raise ValueError('Composed windows require their frozen reader population')
        bundle += '-window-v1'
    discovery = None
    if composed and 'discovery_policy' in body:
        from app.modules.hot_news.scoring.discovery import DiscoveryRanker
        discovery = DiscoveryRanker(body['discovery_policy'])
        bundle += '-' + body['discovery_policy']['version']
    start, end = utc(body['window_start_ms']), utc(body['window_end_ms'])
    contents = [NewsContent(news_id=n, title=title, summary='明确标记的虚构新闻，仅用于流式链路功能验证。',
        content_type=ContentType(kind), publish_time=start-timedelta(hours=1),
        source_url='https://example.invalid/synthetic/'+n,
        body='这是一条本地合成新闻。统计数据来自合成事件，不代表真实读者行为。') for n,(kind,title) in CATALOG.items()]
    runner = NativeHotNewsAnalysisRunner(
        StructuredInferenceService(inference=LocalHotNewsInference(), prompts=local_hot_news_prompts()),
        tenant_id=tenant_id, prompt_version=prompt_version, model_route='native-local-model-v1',
    )
    service = HotNewsOrchestrationService(
        ranker=discovery,
        metric_source=source, baseline_provider=WindowBaselineProvider(body,bundle) if composed else EmptyHotNewsBaselineProvider(),
        enrichment_service=HotNewsEnrichmentService(content_repository=content_repository if public else InMemoryNewsContentRepository(contents), knowledge_search=related_search if related_search is not None else ExplicitEmptyRelatedNews(),
            reader_profile_service=reader_service,reader_profile_window=(start,end) if reader_service else None),
        analysis_service=HotNewsAnalysisService(input_builder=HotNewsAnalysisInputBuilder(policy_version=prompt_version), runner=runner, validator=HotNewsAnalysisValidator()),
        policy=HotNewsOrchestrationPolicy(production_bundle_version=bundle, ranking_limit=3),
        analysis_batch=HotNewsAnalysisBatch(max_concurrency=1),
    )
    request = SnapshotRunRequest(tenant_id=tenant_id,window_start=start,window_end=end,
        production_bundle_version=bundle,snapshot_sha256=snapshot_id,
        retrieval_context_sha256=related_search.identity_sha256 if related_search is not None else None,
        reader_profile_context_sha256=reader_service.context_identity_for_window(tenant_id=tenant_id,window_start=start,window_end=end) if reader_service else None)
    result = await service.run(request)
    return {
        'snapshot_id': snapshot_id, 'run_key': result.idempotency_key, 'tenant_id': tenant_id,
        'final': body['final'], 'source_mode': 'synthetic', 'metric_version': body['metric_version'],
        **({'window_start_ms':body['window_start_ms'],'window_end_ms':body['window_end_ms'],
            'source_refs':body['source_refs'],'baseline':body['baseline'],
            'reader_snapshot_id':body['reader_snapshot']['identity_sha256']} if composed else {}),
        'production_bundle_version': bundle, 'model_mode': 'deterministic_local_port', 'external_model_calls': 0,
        **({'discovery': {'policy':body['discovery_policy'], 'candidates':discovery.evidence,
            'qualified_count':sum(c['qualifies'] for c in discovery.evidence),
            'selected_count':len(result.ranked_news), 'scope':'article_window_candidates'}} if discovery else {}),
        **({'retrieval':related_search.audit()} if related_search is not None else {}),
        **({'content_catalog_id':catalog['catalog_id'], 'content_source_mode':'public_cached',
            'content_release_id':catalog['manifest']['content_release_id'],
            'tag_release_id':catalog['manifest']['tag_release_id'],
            'reader_profile_status':'complete' if reader_service else 'unavailable_for_this_minute_window',
            'event_time_mode':'wall_clock' if composed or body['run_id'].startswith('wc-') else 'accelerated_synthetic',
            'reader_profile_context_sha256':request.reader_profile_context_sha256} if public else {}),
        'ranking': [{'rank':r.rank, 'news_id':r.current.news_id, 'hot_score':str(r.hot_score.score),
            'metrics':{k:getattr(r.current,k) for k in FIELDS}} for r in result.ranked_news],
        'analyses': [{'news_id':a.news_id,'model_request_id':a.analysis.request_id,
            **({'retrieval_evidence':[e.model_dump(mode='json',exclude={'excerpt'}) for e in a.analysis_input.related_news],
                'reader_profile_trace':a.reader_profile_trace} if related_search is not None else {}),
            'report':a.analysis.value.model_dump(mode='json')} for a in result.analyzed_news],
        'limitations': ['公开新闻正文、合成行为与本地模型Port，非企业模型质量验收。' if public else '仅合成数据与本地模型Port，非企业模型质量验收。',
                         ('基线为前一等长完整窗口，属于环比；新发布新闻可能没有可比基线。' if body['baseline']['status']=='complete'
                          else '前一等长窗口早于本次会话，基线不可用，增长分量为零。') if composed else
                         ('缺少历史基线，增长分量为零；关联新闻为版本化 SQL 词面召回，非企业向量检索验收。' if related_search is not None else '缺少历史基线与关联新闻，增长分量为零，未编造检索证据。'),
                         '组合窗口只使用完整分钟；源窗口后续失效时本报告一并标记失效。' if composed else
                         'provisional快照可被迟到数据修订；final表示本实验30秒事件时间迟到预算已结束。'],
    }
