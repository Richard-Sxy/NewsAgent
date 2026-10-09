import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { setImmediate } from 'node:timers/promises'
import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'

function load(source, imports = {}) {
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  })
  const exports = {}
  new Function('require', 'exports', outputText)((name) => {
    assert.ok(Object.hasOwn(imports, name), `Unexpected import: ${name}`)
    return imports[name]
  }, exports)
  return exports
}
const helper = load(readFileSync(new URL('../src/utils/analysisDemo.ts', import.meta.url), 'utf8'))
const format = load(readFileSync(new URL('../src/utils/format.ts', import.meta.url), 'utf8'))
const projection = load(readFileSync(new URL('../src/utils/analysisResult.ts', import.meta.url), 'utf8'), { '@/utils/format': format })
// 由真实 AnalysisRunner 固定 worker 对公开合成输入生成，保持 Python JSON 契约。
const realReports = JSON.parse(readFileSync(new URL('./fixtures/analysis-reports.json', import.meta.url), 'utf8'))
const uuid = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`
function deferred() {
  let resolve
  const promise = new Promise((accept) => { resolve = accept })
  return { promise, resolve }
}

function dependencies() {
  const calls = []
  let windowIndex = 0
  let requestIndex = 10
  const detail = (id) => {
    const index = id === uuid(1) ? 0 : 1
    return { run: { run_id: id, status: 'completed', completed_at: '2026-10-04T00:00:00Z',
      ranked_news_count: 1, ...helper.DEMO_WINDOWS[index] }, ranked_news: [{ news_id: 'public-news' }] }
  }
  const clients = {
    hotNews: {
      runLocalSimulation: async (body) => {
        calls.push(['run', body]); return { run_id: uuid(++windowIndex), status: 'completed' }
      },
      runDetail: async (id) => { calls.push(['detail', id]); return detail(id) },
    },
    conversations: {
      create: async (title) => { calls.push(['create', title]); return { id: uuid(3) } },
      send: async (id, body) => {
        calls.push(['send', id, body])
        const runId = body.content.split(' ').at(-1)
        return { status: 'completed', request_id: body.request_id, user_content: body.content,
          tools: [{ name: 'read_hot_news', status: 'completed', arguments: { run_id: runId },
          result: { run_id: runId, items: [{ news_id: 'public-news' }], ...detail(runId).run } }] }
      },
    },
    newRequestId: () => uuid(++requestIndex),
  }
  return { clients, calls, detail }
}

test('prepare uses exactly two fixed synthetic windows and real reads in a new conversation', async () => {
  const { clients, calls } = dependencies()
  const result = await helper.prepareAnalysisDemo(clients)
  assert.deepEqual(result, { conversationId: uuid(3), referenceRunId: uuid(1), currentRunId: uuid(2) })
  assert.deepEqual(calls.map((call) => call[0]), ['run', 'detail', 'run', 'detail', 'create', 'send', 'send'])
  for (const [index, call] of calls.filter((call) => call[0] === 'run').entries()) {
    assert.deepEqual(call[1], { question: '查询点击量最高的前12条新闻', scenario_id: 'news-ranking', ...helper.DEMO_WINDOWS[index] })
  }
  const sends = calls.filter((call) => call[0] === 'send')
  assert.equal(sends[0][2].content, `读取热点运行 ${uuid(1)}`)
  assert.equal(sends[1][2].content, `读取热点运行 ${uuid(2)}`)
  assert.notEqual(sends[0][2].request_id, sends[1][2].request_id)
  assert.equal(helper.ANALYSIS_EXAMPLES.length, 6)
  assert.equal(new Set(helper.ANALYSIS_EXAMPLES.map((item) => item.operation)).size, 6)
})

test('run, window, source and failed-read rejection stops downstream work without retry', async (t) => {
  for (const kind of ['incomplete', 'window', 'empty', 'same-run', 'read-denied', 'fake-read', 'analysis']) {
    await t.test(kind, async () => {
      const { clients, calls, detail } = dependencies()
      if (kind === 'incomplete') clients.hotNews.runLocalSimulation = async () => {
        calls.push(['run']); return { status: 'processing', run_id: uuid(1) }
      }
      if (['window', 'empty'].includes(kind)) clients.hotNews.runDetail = async (id) => {
        const result = detail(id)
        if (kind === 'window') result.run.window_start = '2026-10-03T21:00:00+08:00'
        else result.ranked_news = []
        return result
      }
      if (kind === 'same-run') {
        clients.hotNews.runLocalSimulation = async () => ({ status: 'completed', run_id: uuid(1) })
        let reads = 0
        clients.hotNews.runDetail = async (id) => ({ ...detail(id), run: {
          ...detail(id).run, ...helper.DEMO_WINDOWS[reads++],
        } })
      }
      if (['read-denied', 'fake-read', 'analysis'].includes(kind)) clients.conversations.send = async (_id, body) => {
        calls.push(['send'])
        return { status: 'completed', request_id: body.request_id, user_content: body.content, tools: kind === 'fake-read' ? [] : [
          { name: 'read_hot_news', status: kind === 'read-denied' ? 'denied' : 'completed',
            arguments: { run_id: uuid(1) },
            result: { run_id: uuid(1), items: [{}], ...helper.DEMO_WINDOWS[0] } },
          ...(kind === 'analysis' ? [{ name: 'analyze_hot_news_data', status: 'completed' }] : []),
        ] }
      }
      await assert.rejects(helper.prepareAnalysisDemo(clients), helper.AnalysisDemoPreparationError)
      assert.ok(calls.filter((call) => call[0] === 'send').length <= 1)
      if (!['read-denied', 'fake-read', 'analysis'].includes(kind)) assert.ok(!calls.some((call) => call[0] === 'create'))
    })
  }
})

test('transport failure is not retried and cancellation prevents late downstream writes', async () => {
  const { clients, calls } = dependencies()
  clients.hotNews.runLocalSimulation = async () => { calls.push(['run']); throw new Error('failure') }
  await assert.rejects(helper.prepareAnalysisDemo(clients), /failure/)
  assert.equal(calls.length, 1)
  const pending = deferred()
  clients.hotNews.runLocalSimulation = () => { calls.push(['run']); return pending.promise }
  const controller = new AbortController()
  const operation = helper.prepareAnalysisDemo({ ...clients, signal: controller.signal })
  controller.abort()
  await assert.rejects(operation, { name: 'AbortError' })
  pending.resolve({ run_id: uuid(1), status: 'completed' })
  await setImmediate()
  assert.equal(calls.length, 2)
  await assert.rejects(helper.prepareAnalysisDemo({ ...clients, signal: controller.signal }), { name: 'AbortError' })
  assert.equal(calls.length, 2)
})

test('cancel during conversation creation or first read prevents remaining source writes', async (t) => {
  for (const stage of ['create', 'read']) await t.test(stage, async () => {
    const { clients, calls } = dependencies()
    const pending = deferred()
    const controller = new AbortController()
    let started
    const entered = new Promise((resolve) => { started = resolve })
    if (stage === 'create') clients.conversations.create = () => {
      calls.push(['create']); started(); return pending.promise
    }
    else clients.conversations.send = () => { calls.push(['send']); started(); return pending.promise }
    const operation = helper.prepareAnalysisDemo({ ...clients, signal: controller.signal })
    await entered
    controller.abort()
    await assert.rejects(operation, { name: 'AbortError' })
    pending.resolve(stage === 'create' ? { id: uuid(3) } : { status: 'completed', tools: [] })
    await setImmediate()
    assert.equal(calls.filter((call) => call[0] === 'send').length, stage === 'create' ? 0 : 1)
  })
})

test('read receipt must match the exact request, target run and window', async (t) => {
  for (const kind of ['request', 'run', 'window']) await t.test(kind, async () => {
    const { clients, calls } = dependencies()
    const original = clients.conversations.send
    clients.conversations.send = async (...args) => {
      const turn = await original(...args)
      if (kind === 'request') turn.request_id = uuid(99)
      if (kind === 'run') turn.tools[0].arguments.run_id = uuid(99)
      if (kind === 'window') turn.tools[0].result.window_end = '2026-10-04T01:00:00+08:00'
      return turn
    }
    await assert.rejects(helper.prepareAnalysisDemo(clients), helper.AnalysisDemoPreparationError)
    assert.equal(calls.filter((call) => call[0] === 'send').length, 1)
  })
})

function enterpriseConfig() {
  return { dataset: { dataset_profile: 'enterprise-v1', dataset_version: 'enterprise-scenarios-v1',
    dataset_sha256: 'a'.repeat(64), enterprise_scenarios: [{ id: 'ranking-churn', label: '榜单进出',
      description: '只比较共同新闻，新增退出不代表全站增长。', question: '查询点击量最高的前5条新闻',
      reference_start: '2026-10-03T14:00:00+08:00', current_start: '2026-10-03T15:00:00+08:00',
      current_end: '2026-10-03T16:00:00+08:00', row_limit: 5, expected_signals: ['cohort_change'] }] } }
}

function scaledConfig(news = 1200) {
  const ids = ['steady', 'breaking', 'fatigue', 'funnel', 'content-mix', 'low-volume',
    'zero-baseline', 'ranking-churn', 'recovery-gap', 'precision']
  const sample = enterpriseConfig().dataset.enterprise_scenarios[0]
  return { schema_version: 'news-warehouse-v2', schema_sha256: 'd'.repeat(64), dataset: {
    schema_version: 'news-warehouse-v2', schema_sha256: 'd'.repeat(64),
    dataset_profile: 'enterprise-v2', dataset_version: 'news-enterprise-scaled-v2', dataset_sha256: 'e'.repeat(64),
    news_per_tenant: news, news_count: news, metric_row_count: news * 24, baseline_row_count: news * 24,
    total_tenants: 2, total_news_count: news * 2, total_metric_row_count: news * 48,
    total_baseline_row_count: news * 48, hours_per_news: 24,
    enterprise_scenarios: ids.map((id) => ({ ...structuredClone(sample), id, row_limit: 100,
      question: '查询点击量最高的前100条新闻' })),
  } }
}

function publicHeadlinesConfig(news = 1200) {
  const config = scaledConfig(news)
  config.schema_version = config.dataset.schema_version = 'news-warehouse-v3'
  Object.assign(config.dataset, { dataset_profile: 'public-headlines-v3', dataset_version: 'news-public-headlines-v3',
    headline_catalog_count: 1200, headline_catalog_sha256: 'c'.repeat(64),
    headline_date_start: '2026-10-01', headline_date_end: '2026-10-07' })
  return config
}

test('public headline catalog keeps v3 identity and only supports sizes covered by actual titles', () => {
  for (const size of [120, 1200]) {
    const catalog = helper.enterpriseDemoCatalog(publicHeadlinesConfig(size))
    assert.equal(catalog.datasetProfile, 'public-headlines-v3')
    assert.equal(catalog.schemaVersion, 'news-warehouse-v3')
    assert.equal(catalog.newsPerTenant, size)
    assert.equal(catalog.headlineCatalogCount, 1200)
    assert.equal(catalog.headlineCatalogSha256, 'c'.repeat(64))
    assert.equal(catalog.headlineDateEnd, '2026-10-07')
  }
  for (const mutate of [
    (config) => { config.dataset.headline_catalog_count = 1199 },
    (config) => { config.dataset.headline_catalog_count = Number.MAX_SAFE_INTEGER + 1 },
    (config) => { config.dataset.headline_catalog_sha256 = 'invalid' },
    (config) => { config.dataset.headline_date_start = '2026-10-08' },
    (config) => { config.dataset.headline_date_end = 'invalid' },
    (config) => { config.schema_version = config.dataset.schema_version = 'news-warehouse-v2' },
  ]) {
    const config = publicHeadlinesConfig(); mutate(config)
    assert.throws(() => helper.enterpriseDemoCatalog(config), helper.AnalysisDemoPreparationError)
  }
  assert.throws(() => helper.enterpriseDemoCatalog(publicHeadlinesConfig(12000)), helper.AnalysisDemoPreparationError)
})

test('public headline preparation stops before submission if the title catalog changes', async () => {
  for (const field of ['headline_catalog_count', 'headline_catalog_sha256', 'headline_date_start', 'headline_date_end']) {
    const config = publicHeadlinesConfig(), saved = helper.enterpriseDemoCatalog(config)
    if (field === 'headline_catalog_count') config.dataset[field]++
    if (field === 'headline_catalog_sha256') config.dataset[field] = 'f'.repeat(64)
    if (field === 'headline_date_start') config.dataset[field] = '2026-09-30'
    if (field === 'headline_date_end') config.dataset[field] = '2026-10-08'
    const { clients, calls } = dependencies()
    clients.hotNews.localSqlConfig = async () => config
    await assert.rejects(helper.prepareAnalysisDemo({ ...clients, enterprise: {
      catalog: saved, scenarioId: 'ranking-churn',
    } }), helper.AnalysisDemoPreparationError)
    assert.equal(calls.length, 0)
  }
})

test('scaled catalog accepts only approved sizes and exact two-tenant, 24-hour counts with Top100 coverage', () => {
  for (const size of [120, 1200, 12000]) {
    const catalog = helper.enterpriseDemoCatalog(scaledConfig(size))
    assert.equal(catalog.datasetProfile, 'enterprise-v2')
    assert.equal(catalog.newsPerTenant, size)
    assert.equal(catalog.totalNewsCount, size * 2)
    assert.equal(catalog.totalMetricRowCount, size * 48)
    assert.equal(catalog.totalBaselineRowCount, size * 48)
    assert.equal(catalog.schemaVersion, 'news-warehouse-v2')
    assert.equal(catalog.scenarios.length, 10)
    assert.ok(catalog.scenarios.every((scenario) => scenario.row_limit === 100))
  }
  const wrongFields = ['news_per_tenant', 'news_count', 'metric_row_count', 'baseline_row_count',
    'total_tenants', 'total_news_count', 'total_metric_row_count', 'total_baseline_row_count', 'hours_per_news']
  for (const field of wrongFields) {
    const config = scaledConfig(); config.dataset[field] += 1
    assert.throws(() => helper.enterpriseDemoCatalog(config), helper.AnalysisDemoPreparationError)
  }
  for (const mutate of [
    (config) => { delete config.dataset.total_metric_row_count },
    (config) => { config.dataset.total_tenants = true },
    (config) => { config.schema_version = 'news-warehouse-v1' },
    (config) => { config.schema_sha256 = 'f'.repeat(64) },
    (config) => { config.dataset.schema_version = 'news-warehouse-v1' },
    (config) => { config.dataset.enterprise_scenarios.pop() },
    (config) => { config.dataset.enterprise_scenarios[0].row_limit = 101 },
    (config) => { config.dataset.enterprise_scenarios[0].row_limit = 99 },
  ]) {
    const config = scaledConfig(); mutate(config)
    assert.throws(() => helper.enterpriseDemoCatalog(config), helper.AnalysisDemoPreparationError)
  }
  const legacy = enterpriseConfig()
  legacy.dataset.enterprise_scenarios[0].row_limit = 13
  legacy.dataset.enterprise_scenarios[0].question = '查询点击量最高的前13条新闻'
  assert.throws(() => helper.enterpriseDemoCatalog(legacy), helper.AnalysisDemoPreparationError)
})

test('scaled preparation keeps both complete Top100 snapshots and the existing five-news read summary', async () => {
  const config = scaledConfig(), scenario = config.dataset.enterprise_scenarios.find((item) => item.id === 'ranking-churn')
  const { clients, calls, detail } = dependencies()
  const windows = [
    { window_start: scenario.reference_start, window_end: scenario.current_start },
    { window_start: scenario.current_start, window_end: scenario.current_end },
  ]
  clients.hotNews.localSqlConfig = async () => config
  clients.hotNews.runDetail = async (id) => {
    const result = detail(id), window = windows[id === uuid(1) ? 0 : 1]
    result.run = { ...result.run, ...window, ranked_news_count: 100 }
    result.ranked_news = Array.from({ length: 100 }, (_, index) => ({ news_id: `scaled-${index}`, rank: index + 1 }))
    result.sql_tool_trace = { preview: { question: scenario.question, scenario_id: 'news-ranking',
      schema_version: config.schema_version, schema_sha256: config.schema_sha256,
      parameters: { ...window, row_limit: 100 } }, result: { truncated: false } }
    return result
  }
  const original = clients.conversations.send
  clients.conversations.send = async (...args) => {
    const turn = await original(...args), result = turn.tools[0].result
    Object.assign(result, windows[result.run_id === uuid(1) ? 0 : 1], { truncated: true,
      items: Array.from({ length: 5 }, (_, index) => ({ news_id: `scaled-${index}`, rank: index + 1 })) })
    return turn
  }
  await helper.prepareAnalysisDemo({ ...clients, enterprise: {
    catalog: helper.enterpriseDemoCatalog(config), scenarioId: 'ranking-churn',
  } })
  assert.equal(calls.filter((call) => call[0] === 'run').length, 2)
  assert.ok(calls.filter((call) => call[0] === 'run').every((call) => call[1].question === '查询点击量最高的前100条新闻'))
  assert.equal(calls.filter((call) => call[0] === 'send').length, 2)
})

test('scaled preparation refuses changed profile, size, schema, version, hash or selected case before submitting', async () => {
  for (const change of ['profile', 'size', 'schema', 'version', 'hash', 'case']) {
    const config = scaledConfig(), saved = helper.enterpriseDemoCatalog(config)
    let refreshed = structuredClone(config)
    if (change === 'profile') refreshed = enterpriseConfig()
    if (change === 'size') refreshed = scaledConfig(120)
    if (change === 'schema') refreshed.schema_sha256 = refreshed.dataset.schema_sha256 = 'f'.repeat(64)
    if (change === 'version') refreshed.dataset.dataset_version = 'next-version'
    if (change === 'hash') refreshed.dataset.dataset_sha256 = 'f'.repeat(64)
    if (change === 'case') refreshed.dataset.enterprise_scenarios.find((item) => item.id === 'ranking-churn').description += '变化'
    const { clients, calls } = dependencies()
    clients.hotNews.localSqlConfig = async () => refreshed
    await assert.rejects(helper.prepareAnalysisDemo({ ...clients, enterprise: {
      catalog: saved, scenarioId: 'ranking-churn',
    } }), helper.AnalysisDemoPreparationError)
    assert.equal(calls.length, 0)
  }
})

test('enterprise catalog accepts fixed case pairs, including gaps, and rejects unsafe or ambiguous configuration', () => {
  assert.equal(helper.enterpriseDemoCatalog({ dataset: { dataset_profile: 'classic-v1' } }), null)
  for (const malformed of [null, {}, [], { dataset: [] }, { dataset: { dataset_profile: 'unknown' } }]) {
    assert.throws(() => helper.enterpriseDemoCatalog(malformed), helper.AnalysisDemoPreparationError)
  }
  assert.equal(helper.enterpriseDemoCatalog({ schema_version: 'news-warehouse-v1', dataset: {
    news_count: 12, metric_row_count: 288, window_start: '2026-10-03T00:00:00+08:00',
    window_end: '2026-10-04T00:00:00+08:00',
  } }), null)
  const config = enterpriseConfig()
  assert.equal(helper.enterpriseDemoCatalog(config).scenarios[0].row_limit, 5)
  for (const mutate of [
    (data) => { data.dataset_sha256 = 'bad' },
    (data) => { data.enterprise_scenarios.push(structuredClone(data.enterprise_scenarios[0])) },
    (data) => { data.enterprise_scenarios[0].current_start = data.enterprise_scenarios[0].reference_start },
    (data) => { data.enterprise_scenarios[0].reference_start = '2026-10-03T14:30:00+08:00' },
    (data) => { data.enterprise_scenarios[0].question = 'select * from private' },
    (data) => { data.enterprise_scenarios[0].row_limit = 1000 },
  ]) {
    const invalid = structuredClone(config); mutate(invalid.dataset)
    assert.throws(() => helper.enterpriseDemoCatalog(invalid), helper.AnalysisDemoPreparationError)
  }
  config.dataset.enterprise_scenarios[0].reference_start = '2026-10-03T12:00:00+08:00'
  assert.equal(helper.enterpriseDemoCatalog(config).scenarios.length, 1)
})

test('enterprise preparation refreshes dataset identity and binds the selected query, windows and real receipts', async () => {
  const config = enterpriseConfig()
  const { clients, calls, detail } = dependencies()
  const windows = [
    { window_start: '2026-10-03T14:00:00+08:00', window_end: '2026-10-03T15:00:00+08:00' },
    { window_start: '2026-10-03T15:00:00+08:00', window_end: '2026-10-03T16:00:00+08:00' },
  ]
  clients.hotNews.localSqlConfig = async () => { calls.push(['config']); return config }
  clients.hotNews.runDetail = async (id) => {
    const result = detail(id), window = windows[id === uuid(1) ? 0 : 1]
    result.run = { ...result.run, ...window }
    result.sql_tool_trace = { preview: { question: '查询点击量最高的前5条新闻', scenario_id: 'news-ranking',
      parameters: { ...window, row_limit: 5 } }, result: { truncated: false } }
    return result
  }
  const originalSend = clients.conversations.send
  clients.conversations.send = async (...args) => {
    const turn = await originalSend(...args)
    Object.assign(turn.tools[0].result, windows[turn.tools[0].result.run_id === uuid(1) ? 0 : 1])
    return turn
  }
  await helper.prepareAnalysisDemo({ ...clients, enterprise: {
    catalog: helper.enterpriseDemoCatalog(config), scenarioId: 'ranking-churn',
  } })
  const runs = calls.filter((call) => call[0] === 'run')
  assert.equal(runs.length, 2)
  runs.forEach((call, index) => {
    assert.equal(call[1].question, '查询点击量最高的前5条新闻')
    assert.equal(Date.parse(call[1].window_start), Date.parse(windows[index].window_start))
    assert.equal(Date.parse(call[1].window_end), Date.parse(windows[index].window_end))
  })
  assert.match(calls.find((call) => call[0] === 'create')[1], /榜单进出/)
  assert.equal(calls.filter((call) => call[0] === 'send').length, 2)
})

test('changing enterprise data or returning a different SQL scope stops before creating a conversation', async () => {
  const config = enterpriseConfig(), saved = helper.enterpriseDemoCatalog(config)
  const { clients, calls } = dependencies()
  clients.hotNews.localSqlConfig = async () => config
  config.dataset.dataset_sha256 = 'b'.repeat(64)
  await assert.rejects(helper.prepareAnalysisDemo({ ...clients, enterprise: {
    catalog: saved, scenarioId: 'ranking-churn',
  } }), helper.AnalysisDemoPreparationError)
  assert.equal(calls.length, 0)
  config.dataset.dataset_sha256 = saved.datasetSha256
  await assert.rejects(helper.prepareAnalysisDemo({ ...clients, enterprise: {
    catalog: saved, scenarioId: 'ranking-churn',
  } }), helper.AnalysisDemoPreparationError)
  assert.ok(!calls.some((call) => call[0] === 'create'))
})

const source = () => ({ run_id: uuid(1), window_start: '2026-10-03T23:00:00+08:00',
  window_end: '2026-10-04T00:00:00+08:00', bundle_version: 'public-v1', row_count: 2,
  scope: 'ranked_news_only', snapshot_sha256: 'a'.repeat(64),
  provenance: { workflow_version: 'workflow-v2', selection_scope_sha256: 'b'.repeat(64) } })
function trace(operation, analysis) {
  return { name: 'analyze_hot_news_data', status: 'completed', result: {
    schema_version: ['baseline', 'trend'].includes(operation) ? '2.0' : '1.0', algorithm_version: 'exact-test',
    operation, metric: 'ctr', analysis, source: source(), limitations: ['榜单范围'],
    execution: { backend: 'process', transport: 'service', elapsed_ms: 8, input_sha256: 'c'.repeat(64),
      limits: { timeout_seconds: 5, max_rows: 200, max_input_bytes: 262144, max_output_bytes: 262144,
        max_concurrency: 2, memory_mb: 128, os_resource_limits_enforced: true } },
  } }
}

test('results require completed analyze receipts and display actual execution and provenance', () => {
  const item = trace('distribution', { count: 2, min: '0.01', max: '0.20', mean: '0.105', median: '0.105', p90: '0.20' })
  const result = projection.projectAnalysisResult(item)
  assert.equal(result.fields.find((field) => field.label === 'P90').value, '20.00%')
  assert.equal(result.source.provenance[1].value, 'b'.repeat(64))
  assert.equal(result.execution.find((field) => field.label === '实际计算后端').value, 'process')
  assert.equal(result.execution.find((field) => field.label === '传输').value, 'service')
  assert.equal(result.execution.find((field) => field.label === '操作系统资源限制').value, '已强制')
  for (const patch of [{ name: 'read_hot_news' }, { status: 'failed' }, { status: 'denied' }]) {
    assert.equal(projection.projectAnalysisResult({ ...item, ...patch }), null)
  }
  for (const version of [1, 2, null, '1', '2', '3.0', true]) {
    assert.equal(projection.projectAnalysisResult({ ...item, result: { ...item.result, schema_version: version } }), null)
  }
  const fake = structuredClone(item)
  fake.result.execution = { backend: 'service' }
  assert.equal(projection.projectAnalysisResult(fake), null)
  delete fake.result.execution
  fake.result.analysis_execution = item.result.execution
  assert.equal(projection.projectAnalysisResult(fake), null)
})

test('all six real Python worker report shapes render with display-only decimal rounding', () => {
  assert.deepEqual(Object.keys(realReports), ['overview', 'distribution', 'compare', 'quality', 'baseline', 'trend'])
  for (const [operation, report] of Object.entries(realReports)) {
    assert.equal(report.schema_version, ['baseline', 'trend'].includes(operation) ? '2.0' : '1.0')
    const result = projection.projectAnalysisResult({ name: 'analyze_hot_news_data', status: 'completed', result: report })
    assert.ok(result, operation)
    assert.equal(result.source.hash, report.source.snapshot_sha256)
    assert.equal(result.execution.find((field) => field.label === '请求 SHA-256').value, report.execution.input_sha256)
    assert.equal(result.execution.find((field) => field.label === '实际计算后端').value, report.execution.backend)
    assert.equal(result.execution.find((field) => field.label === '传输').value, '未声明')
    assert.ok(result.fields.every((field) => field.value !== '未知'), operation)
    assert.equal(result.unsafeInteger, false)
    if (['baseline', 'trend'].includes(operation)) {
      assert.equal(result.rows.length, report.analysis.items.length)
      const value = report.analysis.items[0].current_value
      assert.equal(result.rows[0][1], report.metric === 'ctr' ? format.formatRatioPercent(value)
        : format.formatDecimal(value, report.metric === 'hot_score' ? 4 : 2))
      assert.equal(result.source.provenance[1].value, report.source.provenance.selection_scope_sha256)
    }
  }
})

test('six operations format decimals without changing reports, retain missing values and unsafe integer fallback', () => {
  const overview = projection.projectAnalysisResult(trace('overview', {
    totals: { impressions: Number.MAX_SAFE_INTEGER + 1, clicks: 12, interactions: 1,
      effective_consumptions: 2, total_duration_seconds: 3 }, weighted_ctr: null,
  }))
  assert.equal(overview.fields.find((field) => field.label === '曝光量').value, '见权威答复')
  assert.equal(overview.unsafeInteger, true)
  assert.equal(overview.fields.at(-1).value, '—')
  const compareTrace = trace('compare', {
    highest: { news_id: '新闻A', value: '9007199254740993' }, lowest: { news_id: '新闻B', value: '0' },
    absolute_difference: '9007199254740993', relative_change: null,
  })
  compareTrace.result.metric = 'clicks'
  const compare = projection.projectAnalysisResult(compareTrace)
  assert.ok(compare.fields.some((field) => field.value === '9007199254740993'))
  assert.equal(compare.fields.at(-1).value, '—')
  const quality = projection.projectAnalysisResult(trace('quality', {
    zero_impression_news_ids: ['public-news'], ctr_mismatch_news_ids: [], clicks_above_impressions_news_ids: [], notes: ['质量口径'],
  }))
  assert.equal(quality.fields[0].value, 'public-news')
  assert.deepEqual(quality.notes, ['质量口径'])
  const change = { news_id: 'public-news', current_value: '0.100000000000000000001', reference_value: '0',
    absolute_change: '0.100000000000000000001', relative_change: null }
  const baseline = projection.projectAnalysisResult(trace('baseline', {
    compared_count: 1, missing_baseline_news_ids: [], missing_metric_news_ids: [],
    items: [{ ...change, sample_count: 12, reference_version: 'ref-v1', status: 'compared' }],
  }))
  assert.equal(baseline.rows[0][1], '10.00%')
  assert.equal(change.current_value, '0.100000000000000000001')
  assert.deepEqual(baseline.rows[0].slice(-3), ['12', 'ref-v1', '已比较'])
  const trendTrace = trace('trend', { matched_news_count: 1, gap_seconds: 0, added_news_ids: ['new'],
    removed_news_ids: ['old'], items: [change], aggregate: { method: 'weighted_ctr', ...change } })
  assert.equal(projection.projectAnalysisResult(trendTrace), null)
  trendTrace.result.source.reference = { ...source(), run_id: uuid(2), ...helper.DEMO_WINDOWS[0] }
  const trend = projection.projectAnalysisResult(trendTrace)
  assert.equal(trend.reference.runId, uuid(2))
  assert.equal(trend.rows[0][4], '—')
  assert.equal(trend.fields.find((field) => field.label === '汇总口径').value, '交集按曝光加权点击率')
  assert.equal(trend.fields.find((field) => field.label === '新增榜单新闻').value, 'new')
})

test('reference values and means use two decimal places while scores use four and relative changes use percent', () => {
  const reference = trace('baseline', { compared_count: 1, missing_baseline_news_ids: [], missing_metric_news_ids: [],
    items: [{ news_id: 'public-news', current_value: '123.456789', reference_value: '100.0000000000001',
      absolute_change: '23.456789', relative_change: '0.23456789123', sample_count: 12,
      reference_version: 'ref-v1', status: 'compared' }],
  })
  reference.result.metric = 'clicks'
  const original = structuredClone(reference)
  assert.deepEqual(projection.projectAnalysisResult(reference).rows[0].slice(1, 5), ['123.46', '100', '23.46', '23.46%'])
  assert.deepEqual(reference, original)
  const distribution = trace('distribution', { count: 1, min: '1', max: '2', mean: '1.23456789', median: '1', p90: '2' })
  distribution.result.metric = 'clicks'
  assert.equal(projection.projectAnalysisResult(distribution).fields.find((field) => field.label === '均值').value, '1.23')
  distribution.result.metric = 'hot_score'
  assert.equal(projection.projectAnalysisResult(distribution).fields.find((field) => field.label === '均值').value, '1.2346')
})

class ApiError extends Error { constructor(status) { super('private error'); this.status = status } }
async function panel(t, helperOverride = helper.prepareAnalysisDemo, config = { dataset: { dataset_profile: 'classic-v1' } }) {
  const lifecycle = []
  const events = []
  const props = vue.reactive({ runtime: { model_provider: 'local', query_enabled: true,
    data_analysis: { name: 'analyze_hot_news_data', execution_backend: 'service' } },
  localDemoMode: true, disabled: false, contextKey: 'original' })
  const compiled = compileScript(parse(readFileSync(new URL('../src/components/conversation/AnalysisDemoPanel.vue', import.meta.url), 'utf8')).descriptor, { id: 'analysis-demo-test' }).content
  const component = load(compiled, {
    vue: { ...vue, onBeforeUnmount: (callback) => lifecycle.push(callback) },
    '@/api/conversations': { conversationsApi: {} }, '@/api/hotNews': { hotNewsApi: {
      localSqlConfig: async () => config,
    } },
    '@/api/http': { ApiError }, '@/utils/analysisDemo': { ...helper, prepareAnalysisDemo: helperOverride },
  }).default
  const scope = vue.effectScope()
  const state = scope.run(() => component.setup(props, { expose() {}, emit: (...args) => events.push(args) }))
  const unmount = () => { lifecycle.forEach((callback) => callback()); scope.stop() }
  t.after(unmount)
  await vue.nextTick()
  return { props, state, events, unmount }
}

test('prepare UI requires every authorization flag and also respects parent busy state', async (t) => {
  let calls = 0
  const { props, state } = await panel(t, async () => { calls++; return { conversationId: uuid(3) } })
  for (const mutate of [
    () => { props.localDemoMode = false },
    () => { props.runtime.model_provider = 'openai_compatible' },
    () => { props.runtime.query_enabled = false },
    () => { props.runtime.data_analysis = null },
    () => { props.runtime = null },
    () => { props.disabled = true },
  ]) {
    props.localDemoMode = true; props.disabled = false
    props.runtime = { model_provider: 'local', query_enabled: true, data_analysis: { name: 'analyze_hot_news_data' } }
    mutate(); await state.prepare()
    assert.equal(calls, 0)
  }
  props.runtime = null
  props.runtimeError = 'unavailable'
  assert.match(state.unavailable.value, /无法确认/)
  assert.equal(state.preparationAuthorized.value, false)
})

test('prepare UI rejects concurrent clicks and routes only after completed real preparation', async (t) => {
  const pending = deferred()
  let calls = 0
  const { props, state, events } = await panel(t, () => { calls++; return pending.promise })
  const first = state.prepare()
  await state.prepare()
  assert.equal(calls, 1)
  assert.equal(state.preparing.value, true)
  assert.deepEqual(events, [['busy', true]])
  pending.resolve({ conversationId: uuid(3) }); await first
  assert.deepEqual(events, [['busy', true], ['busy', false], ['prepared', uuid(3), 'original']])
  props.contextKey = uuid(3)
  await vue.nextTick()
  assert.match(state.progress.value, /来源已读取/)
  props.contextKey = 'unrelated'
  await vue.nextTick()
  assert.equal(state.progress.value, null)
  assert.equal(state.error.value, null)
})

test('switching conversations or leaving prevents late route and cancels the signal', async (t) => {
  for (const mode of ['switch', 'leave', 'cancel']) await t.test(mode, async (t) => {
    const pending = deferred()
    let signal
    const { props, state, events, unmount } = await panel(t, (options) => { signal = options.signal; return pending.promise })
    const operation = state.prepare()
    if (mode === 'switch') { props.contextKey = 'different'; await vue.nextTick() }
    if (mode === 'leave') unmount()
    if (mode === 'cancel') state.cancelPreparation()
    assert.equal(signal.aborted, true)
    pending.resolve({ conversationId: uuid(3) }); await operation
    assert.ok(!events.some((event) => event[0] === 'prepared'))
  })
})

test('conflict error stays explicit and does not disclose transport details or retry', async (t) => {
  let calls = 0
  const { state, events } = await panel(t, async () => { calls++; throw new ApiError(409) })
  await state.prepare()
  assert.equal(calls, 1)
  assert.match(state.error.value, /状态冲突/)
  assert.ok(!state.error.value.includes('private'))
  assert.ok(!events.some((event) => event[0] === 'prepared'))
})

test('preparation handler cannot fall back during catalog loading, errors or unknown enterprise selection', async (t) => {
  let calls = 0
  const execute = async () => { calls++; return { conversationId: uuid(3) } }
  const pending = deferred()
  const loading = await panel(t, execute, pending.promise)
  assert.equal(loading.state.catalogLoading.value, true)
  await loading.state.prepare()
  assert.equal(calls, 0)
  pending.resolve({ dataset: { dataset_profile: 'classic-v1' } })
  await setImmediate()
  const invalid = await panel(t, execute, { dataset: { dataset_profile: 'unknown' } })
  assert.ok(invalid.state.catalogError.value)
  await invalid.state.prepare()
  assert.equal(calls, 0)
  const enterprise = await panel(t, execute, enterpriseConfig())
  enterprise.state.selectedScenarioId.value = 'unknown'
  await enterprise.state.prepare()
  assert.equal(calls, 0)
  enterprise.state.selectedScenarioId.value = 'ranking-churn'
  await enterprise.state.prepare()
  assert.equal(calls, 1)
})

test('catalog refresh keeps a valid selected enterprise case', async (t) => {
  const config = enterpriseConfig()
  config.dataset.enterprise_scenarios.unshift({ ...structuredClone(config.dataset.enterprise_scenarios[0]),
    id: 'steady', label: '稳定流量' })
  const { state } = await panel(t, async () => ({}), config)
  state.selectedScenarioId.value = 'ranking-churn'
  await state.loadCatalog()
  assert.equal(state.selectedScenarioId.value, 'ranking-churn')
  assert.equal(state.catalogReady.value, true)
})

test('scaled preparation panel shows per-tenant and total volumes with actual aggregate counts', async (t) => {
  const { state } = await panel(t, async () => ({}), scaledConfig())
  assert.match(state.scaleSummary.value, /每租户 1,200 篇/)
  assert.match(state.scaleSummary.value, /共 2,400 篇/)
  assert.match(state.scaleSummary.value, /2 个租户/)
  assert.match(state.aggregateSummary.value, /每篇 24 小时/)
  assert.match(state.aggregateSummary.value, /指标 57,600 条，基线 57,600 条/)
  assert.equal(state.selectedScenario.value.row_limit, 100)
})

test('public headline preparation panel labels real titles separately from synthetic aggregates', async (t) => {
  const { state } = await panel(t, async () => ({}), publicHeadlinesConfig())
  assert.match(state.scaleSummary.value, /2,400 篇公开标题新闻记录/)
  assert.equal(state.catalog.value.headlineDateStart, '2026-10-01')
  const template = readFileSync(new URL('../src/components/conversation/AnalysisDemoPanel.vue', import.meta.url), 'utf8')
  assert.match(template, /标题发布日期与模拟指标窗口相互独立/)
})

function timelineConfig() {
  const config = scaledConfig(1200)
  config.schema_version = config.dataset.schema_version = 'news-warehouse-v4'
  Object.assign(config.dataset, { dataset_profile: 'timeline-v4', dataset_version: 'news-timeline-20260910-20261009-v1',
    window_start: '2026-09-10T00:00:00+08:00', window_end: '2026-10-10T00:00:00+08:00',
    metric_row_count: 864000, baseline_row_count: 864000, total_metric_row_count: 1728000,
    total_baseline_row_count: 1728000, hours_per_news: 720 })
  const sample = config.dataset.enterprise_scenarios[0]
  for (const id of ['day-over-day', 'week-over-week', 'month-span']) {
    config.dataset.enterprise_scenarios.push({ ...sample, id, reference_start: '2026-09-10T19:00:00+08:00',
      current_start: '2026-10-09T19:00:00+08:00', current_end: '2026-10-09T20:00:00+08:00' })
  }
  return config
}

test('thirty-day catalog accepts scale and rejects altered bounds or scenario windows', () => {
  const config = timelineConfig()
  const catalog = helper.enterpriseDemoCatalog(config)
  assert.equal(catalog.hoursPerNews, 720)
  assert.equal(catalog.metricRowCount, 864000)
  assert.equal(catalog.scenarios.length, 13)
  for (const mutate of [
    (dataset) => { dataset.window_end = '2026-10-11T00:00:00+08:00' },
    (dataset) => { dataset.hours_per_news = 24 },
    (dataset) => { dataset.enterprise_scenarios[0].reference_start = '2026-09-09T00:00:00+08:00' },
    (dataset) => { dataset.enterprise_scenarios[0].current_end = '2026-10-10T01:00:00+08:00' },
  ]) {
    const changed = structuredClone(config)
    mutate(changed.dataset)
    assert.throws(() => helper.enterpriseDemoCatalog(changed), helper.AnalysisDemoPreparationError)
  }
})
