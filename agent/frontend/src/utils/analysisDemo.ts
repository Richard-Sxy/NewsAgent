import type { conversationsApi, ConversationTurn } from '@/api/conversations'
import type { hotNewsApi } from '@/api/hotNews'
import type { HotNewsRunDetailResponse } from '@/api/types'

export const ANALYSIS_EXAMPLES = [
  { operation: 'overview', label: '指标概况', content: '统计这批热点的指标概况' },
  { operation: 'distribution', label: '指标分布', content: '分析这批热点点击率的分布、中位数和P90' },
  { operation: 'compare', label: '高低对比', content: '比较这批热点曝光最高与最低的新闻' },
  { operation: 'quality', label: '数据质量', content: '对这批热点做数据质量检查' },
  { operation: 'baseline', label: '已存基线', content: '比较这批热点点击量与已保存基线' },
  { operation: 'trend', label: '两窗口趋势', content: '比较已读取的两个窗口的点击率趋势' },
] as const

export const DEMO_WINDOWS = [
  { window_start: '2026-10-03T22:00:00+08:00', window_end: '2026-10-03T23:00:00+08:00' },
  { window_start: '2026-10-03T23:00:00+08:00', window_end: '2026-10-04T00:00:00+08:00' },
] as const
const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i

export interface EnterpriseDemoScenario {
  id: string
  label: string
  description: string
  question: string
  reference_start: string
  current_start: string
  current_end: string
  row_limit: number
  expected_signals: string[]
}
export interface EnterpriseDemoCatalog {
  datasetProfile: 'enterprise-v1' | 'enterprise-v2' | 'public-headlines-v3' | 'timeline-v4'
  datasetVersion: string
  datasetSha256: string
  schemaVersion?: string
  schemaSha256?: string
  newsPerTenant: number
  newsCount: number
  metricRowCount: number
  baselineRowCount: number
  totalTenants: number
  totalNewsCount: number
  totalMetricRowCount: number
  totalBaselineRowCount: number
  windowStart: string
  windowEnd: string
  hoursPerNews: number
  headlineCatalogCount?: number
  headlineCatalogSha256?: string
  headlineDateStart?: string
  headlineDateEnd?: string
  scenarios: EnterpriseDemoScenario[]
}
type DemoWindow = { window_start: string; window_end: string }
const HOUR = 3_600_000
const SCALED_IDS = ['steady', 'breaking', 'fatigue', 'funnel', 'content-mix', 'low-volume',
  'zero-baseline', 'ranking-churn', 'recovery-gap', 'precision']
const SCENARIO_FIELDS = ['id', 'label', 'description', 'question', 'reference_start', 'current_start',
  'current_end', 'row_limit', 'expected_signals']

// 仅选择后端发布的固定模拟配置，浏览器不构造新的 SQL 或数据。
export function enterpriseDemoCatalog(config: unknown): EnterpriseDemoCatalog | null {
  if (!config || typeof config !== 'object' || Array.isArray(config)) {
    throw new AnalysisDemoPreparationError('scenario_catalog_invalid')
  }
  const top = config as { dataset?: Record<string, unknown>; schema_version?: unknown; schema_sha256?: unknown }
  const dataset = top.dataset
  if (!dataset || typeof dataset !== 'object' || Array.isArray(dataset)) {
    throw new AnalysisDemoPreparationError('scenario_catalog_invalid')
  }
  if (dataset.dataset_profile === 'classic-v1') return null
  // 兼容扩充前的冻结v1经典实例，同时拒绝未知profile或错误响应。
  if (dataset.dataset_profile == null && top.schema_version === 'news-warehouse-v1'
      && dataset.news_count === 12 && dataset.metric_row_count === 288
      && dataset.window_start === '2026-10-03T00:00:00+08:00'
      && dataset.window_end === '2026-10-04T00:00:00+08:00') return null
  const profile = dataset.dataset_profile
  if (profile !== 'enterprise-v1' && profile !== 'enterprise-v2' && profile !== 'public-headlines-v3' && profile !== 'timeline-v4') throw new AnalysisDemoPreparationError('scenario_catalog_invalid')
  const publicHeadlines = profile === 'public-headlines-v3'
  const timeline = profile === 'timeline-v4'
  const scaled = profile === 'enterprise-v2' || publicHeadlines || timeline
  const hours = timeline ? 720 : 24
  const sampleStart = timeline ? '2026-09-10T00:00:00+08:00' : '2026-10-03T00:00:00+08:00'
  const sampleEnd = timeline ? '2026-10-10T00:00:00+08:00' : '2026-10-04T00:00:00+08:00'
  if (timeline && (dataset.window_start !== sampleStart || dataset.window_end !== sampleEnd)) {
    throw new AnalysisDemoPreparationError('scenario_catalog_invalid')
  }
  const news = scaled ? dataset.news_per_tenant : 12
  if (typeof news !== 'number' || !Number.isInteger(news) || scaled && !(publicHeadlines ? [120, 1200] : [120, 1200, 12000]).includes(news)) {
    throw new AnalysisDemoPreparationError('scenario_catalog_invalid')
  }
  if (publicHeadlines && (typeof dataset.headline_catalog_count !== 'number'
      || !Number.isSafeInteger(dataset.headline_catalog_count) || dataset.headline_catalog_count < news
      || typeof dataset.headline_catalog_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(dataset.headline_catalog_sha256)
      || typeof dataset.headline_date_start !== 'string' || typeof dataset.headline_date_end !== 'string'
      || !/^\d{4}-\d{2}-\d{2}$/.test(dataset.headline_date_start) || !/^\d{4}-\d{2}-\d{2}$/.test(dataset.headline_date_end)
      || !Number.isFinite(Date.parse(dataset.headline_date_start)) || !Number.isFinite(Date.parse(dataset.headline_date_end))
      || dataset.headline_date_start > dataset.headline_date_end)) {
    throw new AnalysisDemoPreparationError('scenario_catalog_invalid')
  }
  const counts = { news_per_tenant: news, news_count: news, metric_row_count: news * hours,
    baseline_row_count: news * hours, total_tenants: 2, total_news_count: news * 2,
    total_metric_row_count: news * hours * 2, total_baseline_row_count: news * hours * 2, hours_per_news: hours }
  if (Object.entries(counts).some(([key, expected]) => (scaled || key in dataset)
      && (!Number.isInteger(dataset[key]) || dataset[key] !== expected))) {
    throw new AnalysisDemoPreparationError('scenario_catalog_invalid')
  }
  const schemaVersion = scaled ? dataset.schema_version : top.schema_version
  const schemaSha256 = scaled ? dataset.schema_sha256 : top.schema_sha256
  if (scaled || schemaVersion != null || schemaSha256 != null) {
    if (schemaVersion !== (timeline ? 'news-warehouse-v4' : publicHeadlines ? 'news-warehouse-v3' : scaled ? 'news-warehouse-v2' : 'news-warehouse-v1')
        || typeof schemaSha256 !== 'string' || !/^[a-f0-9]{64}$/.test(schemaSha256)
        || scaled && (top.schema_version !== schemaVersion || top.schema_sha256 !== schemaSha256)) {
      throw new AnalysisDemoPreparationError('scenario_catalog_invalid')
    }
  }
  if (typeof dataset.dataset_version !== 'string' || !dataset.dataset_version.length
      || typeof dataset.dataset_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(dataset.dataset_sha256)
      || !Array.isArray(dataset.enterprise_scenarios) || !dataset.enterprise_scenarios.length
      || dataset.enterprise_scenarios.length > 24) throw new AnalysisDemoPreparationError('scenario_catalog_invalid')
  const ids = new Set<string>()
  const scenarios = dataset.enterprise_scenarios.map((raw: unknown) => {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      throw new AnalysisDemoPreparationError('scenario_catalog_invalid')
    }
    const item = raw as EnterpriseDemoScenario
    const times = [item.reference_start, item.current_start, item.current_end]
    if (Object.keys(item).length !== SCENARIO_FIELDS.length || !SCENARIO_FIELDS.every((field) => field in item)
        || typeof item.id !== 'string' || !/^[a-z][a-z0-9-]{0,63}$/.test(item.id) || ids.has(item.id)
        || typeof item.label !== 'string' || !item.label.length || item.label.length > 80
        || typeof item.description !== 'string' || item.description.length > 800
        || !Number.isInteger(item.row_limit) || (scaled ? item.row_limit !== 100 : item.row_limit < 1 || item.row_limit > 12)
        || item.question !== `查询点击量最高的前${item.row_limit}条新闻`
        || !Array.isArray(item.expected_signals) || item.expected_signals.length > 16
        || !item.expected_signals.every((value) => typeof value === 'string' && value.length <= 120)
        || !times.every((value) => typeof value === 'string'
          && /^\d{4}-\d{2}-\d{2}T\d{2}:00:00\+08:00$/.test(value) && Number.isFinite(Date.parse(value)))
        || Date.parse(item.current_end) - Date.parse(item.current_start) !== HOUR
        || Date.parse(item.current_start) - Date.parse(item.reference_start) < HOUR
        || Date.parse(item.reference_start) < Date.parse(sampleStart)
        || Date.parse(item.current_end) > Date.parse(sampleEnd)) {
      throw new AnalysisDemoPreparationError('scenario_catalog_invalid')
    }
    ids.add(item.id)
    return { ...item, expected_signals: [...item.expected_signals] }
  })
  const expectedIds = timeline ? [...SCALED_IDS, "day-over-day", "week-over-week", "month-span"] : SCALED_IDS
  if (scaled && (ids.size !== expectedIds.length || !expectedIds.every((id) => ids.has(id)))) {
    throw new AnalysisDemoPreparationError('scenario_catalog_invalid')
  }
  return { datasetProfile: profile, datasetVersion: dataset.dataset_version, datasetSha256: dataset.dataset_sha256,
    ...(typeof schemaVersion === 'string' && typeof schemaSha256 === 'string'
      ? { schemaVersion, schemaSha256 } : {}), newsPerTenant: news, newsCount: news,
    metricRowCount: counts.metric_row_count, baselineRowCount: counts.baseline_row_count, totalTenants: 2,
    totalNewsCount: counts.total_news_count, totalMetricRowCount: counts.total_metric_row_count,
    totalBaselineRowCount: counts.total_baseline_row_count, windowStart: sampleStart, windowEnd: sampleEnd, hoursPerNews: hours, scenarios,
    ...(publicHeadlines ? { headlineCatalogCount: dataset.headline_catalog_count as number,
      headlineCatalogSha256: dataset.headline_catalog_sha256 as string,
      headlineDateStart: dataset.headline_date_start as string, headlineDateEnd: dataset.headline_date_end as string } : {}) }
}

function catalogIdentity(catalog: EnterpriseDemoCatalog): unknown[] {
  return [catalog.datasetProfile, catalog.datasetVersion, catalog.datasetSha256, catalog.schemaVersion,
    catalog.schemaSha256, catalog.newsPerTenant, catalog.newsCount, catalog.metricRowCount,
    catalog.baselineRowCount, catalog.totalTenants, catalog.totalNewsCount, catalog.totalMetricRowCount,
    catalog.totalBaselineRowCount, catalog.windowStart, catalog.windowEnd, catalog.hoursPerNews, catalog.headlineCatalogCount,
    catalog.headlineCatalogSha256, catalog.headlineDateStart, catalog.headlineDateEnd]
}

export class AnalysisDemoPreparationError extends Error {
  constructor(readonly code: string) {
    super('演示来源未通过校验，未继续准备。')
    this.name = 'AnalysisDemoPreparationError'
  }
}

function active(signal?: AbortSignal): void {
  if (signal?.aborted) throw new DOMException('演示准备已取消', 'AbortError')
}

// 热点工作流已开始后可以继续在服务端运行；取消阻止后续写入和迟到跳转。
async function waitActive<T>(operation: () => Promise<T>, signal?: AbortSignal): Promise<T> {
  active(signal)
  if (!signal) return operation()
  let abort: () => void = () => {}
  const cancelled = new Promise<never>((_, reject) => {
    abort = () => reject(new DOMException('演示准备已取消', 'AbortError'))
    signal.addEventListener('abort', abort, { once: true })
  })
  try {
    const result = await Promise.race([operation(), cancelled])
    active(signal)
    return result
  } finally {
    signal.removeEventListener('abort', abort)
  }
}

function verifyRun(detail: HotNewsRunDetailResponse, runId: string, window: DemoWindow): void {
  if (detail.run.run_id !== runId || detail.run.status !== 'completed'
      || !detail.run.completed_at) throw new AnalysisDemoPreparationError('run_not_completed')
  if (Date.parse(detail.run.window_start) !== Date.parse(window.window_start)
      || Date.parse(detail.run.window_end) !== Date.parse(window.window_end)) {
    throw new AnalysisDemoPreparationError('window_mismatch')
  }
  if (!Array.isArray(detail.ranked_news) || !detail.ranked_news.length
      || detail.run.ranked_news_count !== detail.ranked_news.length) {
    throw new AnalysisDemoPreparationError('ranked_source_missing')
  }
}

function verifyRead(turn: ConversationTurn, runId: string, window: DemoWindow, requestId: string): void {
  const read = turn.tools.find((tool) => tool.name === 'read_hot_news'
    && tool.status === 'completed' && tool.result.run_id === runId
    && tool.arguments.run_id === runId && tool.arguments.news_rank == null
    && tool.result.not_found !== true)
  if (turn.status !== 'completed' || turn.request_id !== requestId
      || turn.user_content !== `读取热点运行 ${runId}` || !read || !Array.isArray(read.result.items)
      || !read.result.items.length
      || Date.parse(String(read.result.window_start)) !== Date.parse(window.window_start)
      || Date.parse(String(read.result.window_end)) !== Date.parse(window.window_end)
      || turn.tools.some((tool) => tool.name === 'analyze_hot_news_data')) {
    throw new AnalysisDemoPreparationError('conversation_source_not_bound')
  }
}

export interface PreparedAnalysisDemo {
  conversationId: string
  referenceRunId: string
  currentRunId: string
}

export async function prepareAnalysisDemo(options: {
  hotNews: Pick<typeof hotNewsApi, 'runLocalSimulation' | 'runDetail'>
  conversations: Pick<typeof conversationsApi, 'create' | 'send'>
  signal?: AbortSignal
  onProgress?: (message: string) => void
  newRequestId?: () => string
  enterprise?: { catalog: EnterpriseDemoCatalog; scenarioId: string }
}): Promise<PreparedAnalysisDemo> {
  const { hotNews, conversations, signal, onProgress } = options
  let windows: readonly DemoWindow[] = DEMO_WINDOWS
  let question = '查询点击量最高的前12条新闻'
  let title = '数据分析演示 · 10月3日22–24时'
  let scenario: EnterpriseDemoScenario | undefined
  if (options.enterprise) {
    active(signal)
    const api = hotNews as Pick<typeof hotNewsApi, 'runLocalSimulation' | 'runDetail' | 'localSqlConfig'>
    if (typeof api.localSqlConfig !== 'function') throw new AnalysisDemoPreparationError('scenario_catalog_unavailable')
    const catalog = enterpriseDemoCatalog(await waitActive(() => api.localSqlConfig({ signal }), signal))
    const selected = catalog?.scenarios.find((item) => item.id === options.enterprise!.scenarioId)
    const previous = options.enterprise.catalog.scenarios.find((item) => item.id === options.enterprise!.scenarioId)
    if (!catalog || !selected || !previous
        || JSON.stringify(catalogIdentity(catalog)) !== JSON.stringify(catalogIdentity(options.enterprise.catalog))
        || SCENARIO_FIELDS.some((field) => JSON.stringify(selected[field as keyof EnterpriseDemoScenario])
          !== JSON.stringify(previous[field as keyof EnterpriseDemoScenario]))) {
      throw new AnalysisDemoPreparationError('scenario_dataset_changed')
    }
    windows = [
      { window_start: selected.reference_start,
        window_end: new Date(Date.parse(selected.reference_start) + HOUR).toISOString() },
      { window_start: selected.current_start, window_end: selected.current_end },
    ]
    question = selected.question
    title = `企业情形模拟 · ${selected.label}`
    scenario = selected
  }
  const runIds: string[] = []
  for (const [index, window] of windows.entries()) {
    onProgress?.(`正在创建或复用第 ${index + 1} 个合成热点窗口…`)
    const result = await waitActive(() => hotNews.runLocalSimulation({
      question, scenario_id: 'news-ranking', ...window,
    }), signal)
    if (result.status !== 'completed' || !UUID.test(result.run_id)) {
      throw new AnalysisDemoPreparationError('run_not_completed')
    }
    const detail = await waitActive(() => hotNews.runDetail(result.run_id), signal)
    verifyRun(detail, result.run_id, window)
    if (scenario) {
      const trace = detail.sql_tool_trace
      if (!trace || trace.preview.question !== question || trace.preview.scenario_id !== 'news-ranking'
          || options.enterprise?.catalog.schemaVersion != null
            && (trace.preview.schema_version !== options.enterprise.catalog.schemaVersion
              || trace.preview.schema_sha256 !== options.enterprise.catalog.schemaSha256)
          || trace.preview.parameters.row_limit !== scenario.row_limit
          || Date.parse(String(trace.preview.parameters.window_start)) !== Date.parse(window.window_start)
          || Date.parse(String(trace.preview.parameters.window_end)) !== Date.parse(window.window_end)
          || detail.ranked_news.length > scenario.row_limit || trace.result.truncated) {
        throw new AnalysisDemoPreparationError('scenario_source_mismatch')
      }
    }
    runIds.push(result.run_id)
  }
  if (runIds[0] === runIds[1]) throw new AnalysisDemoPreparationError('same_run')
  onProgress?.('正在创建独立演示会话，并读取两个窗口的真实来源…')
  const conversation = await waitActive(() => conversations.create(
    title, { signal },
  ), signal)
  if (!UUID.test(conversation.id)) throw new AnalysisDemoPreparationError('conversation_invalid')
  const newRequestId = options.newRequestId ?? (() => crypto.randomUUID())
  for (const [index, runId] of runIds.entries()) {
    const requestId = newRequestId()
    if (!UUID.test(requestId)) throw new AnalysisDemoPreparationError('request_id_invalid')
    const turn = await waitActive(() => conversations.send(conversation.id, {
      request_id: requestId, content: `读取热点运行 ${runId}`,
    }, { signal }), signal)
    verifyRead(turn, runId, windows[index]!, requestId)
  }
  active(signal)
  return { conversationId: conversation.id, referenceRunId: runIds[0]!, currentRunId: runIds[1]! }
}
