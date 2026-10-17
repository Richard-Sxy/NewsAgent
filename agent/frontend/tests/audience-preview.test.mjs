import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
import * as vue from 'vue'

const apiSource = readFileSync(new URL('../src/api/audience.ts', import.meta.url), 'utf8')
const panelSource = readFileSync(new URL('../src/components/hotnews/AudiencePreviewPanel.vue', import.meta.url), 'utf8')

class ApiError extends Error {
  constructor(status, detail = 'private upstream response') {
    super(detail)
    this.status = status
  }
}

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

const api = load(apiSource, { '@/api/http': { request: async () => undefined } })
const clone = (value) => structuredClone(value)
const catalog = {
  schema_version: 1, taxonomy_version: 'topics-v1', status: 'draft', source_mode: 'synthetic',
  topics: [
    { topic_id: 'technology', name: '科技', parent_topic_id: null, enabled: true, provenance: 'fixture' },
    { topic_id: 'ai', name: '人工智能', parent_topic_id: 'technology', enabled: true, provenance: 'fixture' },
    { topic_id: 'disabled', name: '未开放主题', parent_topic_id: 'technology', enabled: false, provenance: 'fixture' },
  ],
}
const selection = { taxonomy_version: 'topics-v1', include_topic_ids: ['technology', 'ai'], exclude_topic_ids: [], match: 'any' }
function response(body = selection, overrides = {}) {
  return {
    preview_only: true, manual_review_required: true, dispatch_enabled: false,
    run_id: 'run-1', news_id: 'news-1', selection: clone(body), fingerprint: 'preview-fingerprint',
    snapshot: {
      source_mode: 'synthetic', run_id: 'run-1', news_id: 'news-1', matched_users: 1200, eligible_users: 800,
      snapshot_id: 'snapshot-1', profile_version: 'profiles-v1', source_version: 'source-v1',
      generated_at: new Date(Date.now() - 1000).toISOString(), expires_at: new Date(Date.now() + 60_000).toISOString(),
      watermark: '2026-10-16T08:00:00Z', window_start: '2026-10-15T08:00:00Z', window_end: '2026-10-16T08:00:00Z',
    },
    recommendation: { action: 'consider', reason: '按服务端聚合范围交给运营判断。' }, limitations: ['模拟数据不能代表企业真实用户。'],
    ...overrides,
  }
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((accept, decline) => { resolve = accept; reject = decline })
  return { promise, resolve, reject }
}

function panel(t, overrides = {}) {
  const calls = { topics: [], preview: [] }
  const unmounted = []
  const audienceApi = {
    topics: async (signal) => {
      calls.topics.push(signal)
      return overrides.topics ? overrides.topics(signal) : clone(catalog)
    },
    preview: async (runId, newsId, body, signal) => {
      calls.preview.push({ runId, newsId, body, signal })
      return overrides.preview ? overrides.preview(runId, newsId, body, signal) : response(body, { run_id: runId, news_id: newsId })
    },
  }
  const imports = {
    vue: { ...vue, onBeforeUnmount: (callback) => unmounted.push(callback) },
    '@/api/audience': { audienceApi }, '@/api/http': { ApiError },
    '@/utils/format': { formatDateTime: (value) => value, formatDecimal: (value) => String(value) },
  }
  const component = load(compileScript(parse(panelSource).descriptor, { id: 'audience-preview-test' }).content, imports).default
  const props = vue.reactive({ runId: 'run-1', newsId: 'news-1' })
  const scope = vue.effectScope()
  const state = scope.run(() => component.setup(props, { expose() {} }))
  const unmount = () => { unmounted.forEach((callback) => callback()); scope.stop() }
  t.after(unmount)
  return { props, state, calls, unmount, imports }
}

test('catalog keeps a coherent two-level hierarchy and rejects duplicate, orphan, and third-level topics', () => {
  assert.deepEqual(api.parseAudienceTopics(catalog), catalog)
  for (const change of [
    (value) => value.topics.push(clone(value.topics[0])),
    (value) => { value.topics[1].parent_topic_id = 'missing' },
    (value) => { value.topics[2].parent_topic_id = 'ai' },
    (value) => { value.topics[0].parent_topic_id = 'ai' },
    (value) => { value.source_mode = 'unknown' },
  ]) {
    const value = clone(catalog)
    change(value)
    assert.throws(() => api.parseAudienceTopics(value), api.AudienceResponseError)
  }
})

test('response binding accepts server-normalized set order but rejects wrong news, selection, counts or dispatch claims', () => {
  const valid = response({ ...selection, include_topic_ids: ['ai', 'technology'] })
  assert.equal(api.parseAudiencePreview(valid, 'run-1', 'news-1', selection), valid)
  for (const change of [
    (value) => { value.run_id = 'other-run' },
    (value) => { value.snapshot.news_id = 'other-news' },
    (value) => { value.dispatch_enabled = true },
    (value) => { value.manual_review_required = false },
    (value) => { value.selection.match = 'all' },
    (value) => { value.selection.include_topic_ids = ['ai'] },
    (value) => { value.selection.include_topic_ids = ['ai', 'ai'] },
    (value) => { value.snapshot.eligible_users = 1201 },
    (value) => { value.snapshot.matched_users = -1 },
    (value) => { value.snapshot.matched_users = 0.5 },
    (value) => { value.snapshot.matched_users = Number.MAX_SAFE_INTEGER + 1 },
    (value) => { value.snapshot.expires_at = value.snapshot.generated_at },
    (value) => { value.snapshot.window_start = 'invalid' },
    (value) => { value.recommendation.action = 'dispatch' },
  ]) {
    const value = response()
    change(value)
    assert.throws(() => api.parseAudiencePreview(value, 'run-1', 'news-1', selection), api.AudienceResponseError)
  }
})

test('API encodes route IDs and sends only the selection through the shared cookie client', async () => {
  const calls = []
  const client = load(apiSource, { '@/api/http': { request: async (path, options) => {
    calls.push({ path, options })
    return path.endsWith('/topics') ? clone(catalog) : response(selection, {
      run_id: 'run/1', news_id: 'news?1', snapshot: { ...response().snapshot, run_id: 'run/1', news_id: 'news?1' },
    })
  } } }).audienceApi
  const controller = new AbortController()
  await client.topics(controller.signal)
  await client.preview('run/1', 'news?1', selection, controller.signal)
  assert.equal(calls[0].path, '/api/v1/audience/topics')
  assert.equal(calls[1].path, '/api/v1/audience/runs/run%2F1/news/news%3F1/preview')
  assert.deepEqual(calls[1].options, { method: 'POST', body: selection, signal: controller.signal })
  assert.equal(Object.hasOwn(calls[1].options.body, 'tenant_id'), false)
  assert.equal(Object.hasOwn(calls[1].options, 'headers'), false)
})

test('panel loads only on demand, renders hierarchy, and sends exclusive include/exclude selection', async (t) => {
  const { state, calls } = panel(t)
  assert.equal(calls.topics.length, 0)
  assert.equal(calls.preview.length, 0)
  await state.loadCatalog()
  assert.equal(state.groups.value[0].root.name, '科技')
  assert.deepEqual(state.groups.value[0].children.map((topic) => topic.name), ['人工智能', '未开放主题'])
  state.toggleTopic('disabled', 'include')
  assert.deepEqual(state.includeTopicIds.value, [])
  state.toggleTopic('ai', 'include')
  state.toggleTopic('ai', 'exclude')
  assert.deepEqual(state.includeTopicIds.value, [])
  assert.deepEqual(state.excludeTopicIds.value, ['ai'])
  state.toggleTopic('technology', 'include')
  state.match.value = 'all'
  await state.estimateAudience()
  assert.deepEqual(calls.preview[0].body, { taxonomy_version: 'topics-v1', include_topic_ids: ['technology'], exclude_topic_ids: ['ai'], match: 'all' })
  assert.equal(state.preview.value.snapshot.eligible_users, 800)
  state.toggleTopic('ai', 'include')
  assert.equal(state.preview.value, null)
  assert.deepEqual(state.excludeTopicIds.value, [])
})

test('new request clears old counts and selection changes cancel pending work, ignoring late responses', async (t) => {
  const delayed = deferred()
  let count = 0
  const { state, calls } = panel(t, { preview: async (_runId, _newsId, body) => ++count === 1 ? response(body) : delayed.promise })
  await state.loadCatalog()
  state.toggleTopic('ai', 'include')
  await state.estimateAudience()
  assert.ok(state.preview.value)
  const pending = state.estimateAudience()
  assert.equal(state.preview.value, null)
  assert.equal(state.previewPending.value, true)
  state.match.value = 'all'
  assert.equal(calls.preview[1].signal.aborted, true)
  delayed.resolve(response(calls.preview[1].body))
  await pending
  assert.equal(state.preview.value, null)
  assert.equal(state.previewPending.value, false)
})

for (const prop of ['runId', 'newsId']) {
  test(`changing ${prop} invalidates the directory and ignores a late estimate`, async (t) => {
    const delayed = deferred()
    const { state, calls, props } = panel(t, { preview: () => delayed.promise })
    await state.loadCatalog()
    state.toggleTopic('ai', 'include')
    const pending = state.estimateAudience()
    props[prop] = 'different'
    assert.equal(state.catalog.value, null)
    assert.equal(calls.preview[0].signal.aborted, true)
    delayed.resolve(response(calls.preview[0].body))
    await pending
    assert.equal(state.preview.value, null)
    assert.deepEqual(state.includeTopicIds.value, [])
  })
}

test('directory refresh and unmount ignore late data rather than resurrecting prior state', async (t) => {
  const first = deferred()
  let count = 0
  const { state, calls, unmount } = panel(t, { topics: () => ++count === 1 ? first.promise : Promise.resolve(clone(catalog)) })
  const old = state.loadCatalog()
  await state.loadCatalog()
  assert.equal(calls.topics[0].aborted, true)
  first.resolve({ ...catalog, taxonomy_version: 'old-version' })
  await old
  assert.equal(state.catalog.value.taxonomy_version, 'topics-v1')
  unmount()
  assert.equal(calls.topics[1].aborted, true)
})

test('backend permission and unavailable errors show safe business messages without upstream details', async (t) => {
  for (const status of [403, 503, 500]) {
    const { state } = panel(t, { preview: async () => { throw new ApiError(status) } })
    await state.loadCatalog()
    state.toggleTopic('ai', 'include')
    await state.estimateAudience()
    assert.equal(state.preview.value, null)
    assert.ok(!state.previewError.value.includes('private'))
    if (status === 403) {
      assert.match(state.previewError.value, /运营决策权限/)
      assert.equal(state.canPreview.value, false)
    }
    if (status === 503) assert.match(state.previewError.value, /尚未启用.*原热点分析/)
  }
  const { state } = panel(t, { topics: async () => { throw new ApiError(403) } })
  await state.loadCatalog()
  assert.match(state.catalogError.value, /没有查看主题目录的权限/)
})

test('expired server snapshot is explicitly stale; no frontend sum or substitution of counts', async (t) => {
  const { state } = panel(t, { preview: async (_runId, _newsId, body) => {
    const value = response(body)
    value.snapshot.generated_at = new Date(Date.now() - 20_000).toISOString()
    value.snapshot.expires_at = new Date(Date.now() - 10_000).toISOString()
    return value
  } })
  await state.loadCatalog()
  state.toggleTopic('ai', 'include')
  await state.estimateAudience()
  assert.equal(state.expired.value, true)
  assert.equal(state.preview.value.snapshot.matched_users, 1200)
  assert.equal(state.preview.value.snapshot.eligible_users, 800)
  assert.match(panelSource, /此预估已过期/)
})

test('a valid estimate expires in place when its server deadline passes', async (t) => {
  t.mock.timers.enable({ apis: ['Date', 'setTimeout'], now: Date.now() })
  const { state } = panel(t)
  await state.loadCatalog()
  state.toggleTopic('ai', 'include')
  await state.estimateAudience()
  assert.equal(state.expired.value, false)
  t.mock.timers.tick(60_001)
  assert.equal(state.expired.value, true)
})

test('late prior estimates cannot clear a newer request or replace its result', async (t) => {
  const first = deferred()
  const second = deferred()
  let count = 0
  const { state, calls } = panel(t, { preview: () => ++count === 1 ? first.promise : second.promise })
  await state.loadCatalog()
  state.toggleTopic('ai', 'include')
  const old = state.estimateAudience()
  state.match.value = 'all'
  const current = state.estimateAudience()
  first.resolve(response(calls.preview[0].body))
  await old
  assert.equal(state.preview.value, null)
  assert.equal(state.previewPending.value, true)
  second.resolve(response(calls.preview[1].body, { fingerprint: 'newest' }))
  await current
  assert.equal(state.preview.value.fingerprint, 'newest')
  assert.equal(state.preview.value.selection.match, 'all')
})

test('selected topics are bounded by the same ten-item limit as the backend', async (t) => {
  const wide = clone(catalog)
  wide.topics.push(...Array.from({ length: 10 }, (_, index) => ({
    topic_id: `topic-${index}`, name: `主题${index}`, parent_topic_id: null, enabled: true, provenance: 'fixture',
  })))
  const { state } = panel(t, { topics: async () => wide })
  await state.loadCatalog()
  for (const topic of wide.topics.filter((topic) => topic.enabled)) state.toggleTopic(topic.topic_id, 'include')
  assert.equal(state.includeTopicIds.value.length, 10)
  assert.match(state.selectionError.value, /最多选择10个主题/)
})

test('rendered initial panel is an opt-in preview with no send control or automatic request', async (t) => {
  const { imports, calls } = panel(t)
  const component = load(compileScript(parse(panelSource).descriptor, { id: 'audience-render-test', inlineTemplate: true }).content,
    { ...imports, vue }).default
  const html = await renderToString(vue.createSSRApp(component, { runId: 'run-1', newsId: 'news-1' }))
  assert.match(html, /选择受众主题/)
  assert.match(html, /仅预估，不发送推送/)
  assert.equal(calls.topics.length, 0)
  assert.equal(calls.preview.length, 0)
})

test('rendered estimate shows parent and subtopic choices, source labels, server counts and review boundaries', async (t) => {
  const { imports } = panel(t)
  const descriptor = parse(panelSource).descriptor
  const script = compileScript(descriptor, { id: 'audience-filled-test' })
  const component = load(script.content, imports).default
  const template = compileTemplate({ id: 'audience-filled-test', source: descriptor.template.content,
    filename: 'AudiencePreviewPanel.vue', compilerOptions: { bindingMetadata: script.bindings } })
  assert.deepEqual(template.errors, [])
  component.render = load(template.code, imports).render
  const originalSetup = component.setup
  component.setup = async (props, context) => {
    const state = originalSetup(props, context)
    await state.loadCatalog()
    state.toggleTopic('ai', 'include')
    await state.estimateAudience()
    return state
  }
  const html = await renderToString(vue.createSSRApp(component, { runId: 'run-1', newsId: 'news-1' }))
  assert.match(html, /科技/)
  assert.match(html, /人工智能/)
  assert.match(html, /未开放主题.*未开放/)
  assert.match(html, /模拟受众人数/)
  assert.match(html, /匹配受众（去重）/)
  assert.match(html, /符合通知资格（去重快照）/)
  assert.match(html, /1200/)
  assert.match(html, /800/)
  assert.match(html, /不代表发送承诺或审批通过/)
})
