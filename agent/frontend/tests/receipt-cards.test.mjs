/* eslint vue/one-component-per-file: "off" -- These render tests intentionally compile several component fixtures. */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { setImmediate } from 'node:timers/promises'
import { compileScript, parse } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
import * as vue from 'vue'

const source = (path) => readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8')
function load(code, imports = {}, globals = {}) {
  const { outputText } = ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } })
  const exports = {}
  new Function('require', 'exports', ...Object.keys(globals), outputText)((name) => {
    assert.ok(Object.hasOwn(imports, name), `Unexpected import ${name}`)
    return imports[name]
  }, exports, ...Object.values(globals))
  return exports
}
const admission = load(source('utils/admission.ts'))
const receipts = load(source('utils/capabilityReceipts.ts'), { '@/utils/admission': admission })
const labels = load(source('utils/labels.ts'))
const confirmations = load(source('utils/capabilityConfirmation.ts'), { '@/utils/capabilityReceipts': receipts, '@/utils/labels': labels, '@/utils/admission': admission })
const formats = { formatDateTime: (value) => value }
const ID = '12345678-1234-5678-9012-123456789012'
const OTHER = '12345678-1234-5678-9012-123456789013'
const NOW = Date.parse('2026-10-13T00:00:00Z')
const receipt = (entity_type, patch = {}) => ({ entity_type, entity_id: ID, status: 'landed', version: '1',
  deep_link: entity_type === 'feedback_case' ? `/data-loop?case_id=${ID}` : null,
  pending_gate: null, created_at: '2026-10-13T00:00:00Z', ...patch })
const memory = (patch = {}) => ({ id: ID, content: '关注科技频道', expires_at: '2026-10-14T00:00:00Z', tier: 'short_term',
  candidate_status: null, status: 'active', version: '1', not_promoted: false, ...patch })
const feedback = (patch = {}) => ({ case_id: ID, run_id: 'hot-run-saved', news_id: 'news-saved', production_bundle_version: 'bundle-v1',
  issue_type: 'analysis_incorrect', expected_label_draft: 'fact-mismatch', status: 'needs_label', ...patch })
const proposal = (patch = {}) => ({ token: 'opaque_feedback_token_1234567890', hook: 'save_feedback_case',
  expires_at: '2026-10-13T00:01:00Z', input_request_id: ID, state: 'pending', provenance: 'model_suggestion',
  suggested: { run_id: 'hot-run-saved', news_id: 'news-saved', issue_type: 'analysis_incorrect', expected_label_draft: 'fact-mismatch' },
  fields: [{ label: '热点运行 ID', value: 'hot-run-saved' }, { label: '新闻 ID', value: 'news-saved' },
    { label: '问题类型', value: '分析有误' }, { label: '期望标签草稿', value: 'fact-mismatch' }], ...patch })
const trace = (result, patch = {}) => ({ name: 'save_feedback_case', status: 'completed', attempts: 1, arguments: {}, result, error_code: null, ...patch })

const RouterLink = (props, { slots }) => vue.h('a', { href: props.to }, slots.default?.())
RouterLink.props = ['to']
function component(path, imports, inlineTemplate = true) {
  return load(compileScript(parse(source(path)).descriptor, { id: path, inlineTemplate }).content, { vue, ...imports }).default
}
const BaseCard = component('components/conversation/CapabilityReceiptCard.vue', {
  'vue-router': { RouterLink }, '@/utils/capabilityReceipts': receipts, '@/utils/format': formats,
})
const MemoryCard = component('components/conversation/MemoryCard.vue', {
  '@/components/conversation/CapabilityReceiptCard.vue': { default: BaseCard }, '@/utils/format': formats,
})
const FeedbackCard = component('components/conversation/FeedbackCaseCard.vue', {
  '@/components/conversation/CapabilityReceiptCard.vue': { default: BaseCard }, '@/utils/labels': labels,
})
const render = (card, props) => renderToString(vue.createSSRApp({ render: () => vue.h(card, props) }))

for (const [name, Card, snapshot] of [['memory', MemoryCard, memory()], ['feedback_case', FeedbackCard, feedback()]]) {
  test(`${name} card renders all receipt states and bound metadata without approval actions`, async () => {
    for (const status of ['landed', 'pending_approval', 'not_landed']) {
      const value = receipt(name, status === 'not_landed' ? { status, entity_id: null, version: null, deep_link: null } : { status })
      const html = await render(Card, { receipt: value, snapshot: status === 'not_landed' ? null : snapshot })
      assert.match(html, new RegExp(`na-receipt__badge--${status}`))
      assert.doesNotMatch(html, /<button|批准|晋升按钮|二审通过/)
      if (status !== 'not_landed') assert.match(html, new RegExp(name === 'memory' ? '关注科技频道' : 'bundle-v1'))
    }
  })
}

test('long-term memory candidates explicitly remain unpromoted and never link to a nonexistent management page', async () => {
  const html = await render(MemoryCard, { receipt: receipt('memory', { status: 'pending_approval', pending_gate: 'promotion' }),
    snapshot: memory({ tier: 'long_term_candidate', candidate_status: 'pending', status: 'pending', expires_at: null, not_promoted: true }) })
  assert.match(html, /待人工审批/)
  assert.match(html, /长期记忆候选/)
  assert.match(html, /等待人工晋升后才作为长期记忆使用/)
  assert.doesNotMatch(html, /href=|<button/)
})

test('expired memory candidates use the authoritative effective status and do not claim a pending promotion', async () => {
  const html = await render(MemoryCard, { receipt: receipt('memory'), snapshot: memory({ tier: 'long_term_candidate',
    candidate_status: 'pending', status: 'pending', effective_status: 'expired', not_promoted: true }) })
  assert.match(html, /已过期/)
  assert.match(html, /尚未晋升为长期记忆/)
  assert.doesNotMatch(html, /等待人工晋升后/)
})

test('saved feedback cards render truthful rejection and draft semantics while escaping content', async () => {
  const html = await render(FeedbackCard, { receipt: receipt('feedback_case', { status: 'pending_approval' }),
    snapshot: feedback({ expected_label_draft: '<script>draft</script>' }) })
  assert.match(html, /同时记录本次分析驳回反馈；期望标签仅草稿/)
  assert.match(html, /&lt;script&gt;draft&lt;\/script&gt;/)
  assert.match(html, new RegExp(`href="/data-loop\\?case_id=${ID}"`))
  assert.doesNotMatch(html, /<script>|<button/)
})

test('tool receipt extraction binds memories and cases by canonical entity ID and ignores model text', () => {
  assert.equal(receipts.toolReceipts(trace({ receipt: receipt('memory'), memory: memory() }, { name: 'save_short_term_memory' }))[0].memory.content, '关注科技频道')
  assert.equal(receipts.toolReceipts(trace({ receipt: receipt('memory'), memory: memory({ id: OTHER }) }))[0].memory, null)
  assert.equal(receipts.toolReceipts(trace({ receipt: receipt('feedback_case'), feedback: feedback() }))[0].feedback.case_id, ID)
  assert.equal(receipts.toolReceipts(trace({ receipt: receipt('feedback_case'), feedback: feedback({ case_id: OTHER }) }))[0].feedback, null)
  assert.deepEqual(receipts.toolReceipts(trace({ assistant_content: JSON.stringify(receipt('memory')) })), [])
  assert.equal(receipts.parseMemorySnapshot(memory({ expires_at: 'not-a-date' }), ID), null)
})

test('unknown receipts fall back to read-only JSON and cannot expose approval or arbitrary navigation', async () => {
  const html = await render(BaseCard, { receipt: receipt('unrecognized', { deep_link: 'javascript:alert(1)' }) })
  assert.match(html, /只读数据/)
  assert.doesNotMatch(html, /href=|<button/)
})

test('generic feedback confirmation accepts only saved hook snapshots, whitelisted problem types and text fields', () => {
  const pending = receipt('feedback_case', { status: 'not_landed', entity_id: null, version: null, deep_link: null })
  assert.deepEqual(confirmations.capabilityConfirmation(trace({ receipt: pending, confirmation: proposal() })), proposal())
  for (const value of [proposal({ hook: 'approve' }), proposal({ provenance: 'model_text' }), proposal({ fields: [{ label: '新闻', value: {} }] }),
    proposal({ suggested: { ...proposal().suggested, issue_type: 'approve_label' } }), proposal({ input_request_id: 'bad-id' })]) {
    assert.equal(confirmations.capabilityConfirmation(trace({ receipt: pending, confirmation: value })), null)
  }
  assert.equal(confirmations.capabilityConfirmationOutcome([trace({ receipt: receipt('feedback_case'), feedback: feedback(), confirmation_outcome: 'saved' })], 'save_feedback_case'), 'completed')
  assert.equal(confirmations.capabilityConfirmationOutcome([trace({ receipt: receipt('feedback_case') }, { name: 'start_writing_intent' })], 'save_feedback_case'), null)
})

function controls(t, options = {}) {
  let now = NOW
  const calls = []
  const emitted = []
  const timers = new Map()
  const unmounts = []
  let sequence = 0
  const props = vue.reactive({ confirmation: proposal(), conversationId: 'conversation-saved', turnId: 'turn-saved', active: true,
    enabled: true, outcome: null, inputSubmitted: false, originalContent: '沉淀为案例', ...options.props })
  const control = load(source('composables/useCapabilityConfirmation.ts'), {
    vue: { ...vue, onBeforeUnmount: (callback) => unmounts.push(callback) },
    '@/api/conversations': { conversationsApi: { appendInput: async (...arguments_) => {
      calls.push(arguments_)
      return options.append ? options.append(...arguments_) : { turn_id: props.turnId, request_id: ID, status: 'pending' }
    } } },
    '@/api/http': { ApiError: class extends Error {} },
  }, { Date: { now: () => now, parse: Date.parse }, setTimeout: (callback) => { timers.set(++sequence, callback); return sequence },
    clearTimeout: (id) => timers.delete(id) })
  const scope = vue.effectScope()
  const state = scope.run(() => control.useCapabilityConfirmation(props, (content) => emitted.push(content)))
  t.after(() => { for (const unmount of unmounts) unmount(); scope.stop() })
  const Card = component('components/conversation/CapabilityConfirmationCard.vue', {
    '@/composables/useCapabilityConfirmation': { useCapabilityConfirmation: () => state }, '@/utils/format': options.formats ?? formats, '@/utils/labels': labels,
  })
  return { state, props, calls, emitted, timers, render: () => render(Card, props), expire: async () => {
    now = NOW + 60_001; for (const callback of timers.values()) callback(); timers.clear(); await vue.nextTick()
  } }
}

test('generic confirmation renders server text fields, explicit rejection notice and permission-controlled saving', async (t) => {
  const card = controls(t, { props: { confirmation: proposal({ fields: [{ label: '<b>新闻</b>', value: '<script>saved</script>' }] }) } })
  const html = await card.render()
  assert.match(html, /&lt;b&gt;新闻&lt;\/b&gt;/)
  assert.match(html, /&lt;script&gt;saved&lt;\/script&gt;/)
  assert.match(html, /同时记录本次分析驳回反馈；期望标签仅草稿/)
  assert.match(html, /确认保存/)
  assert.doesNotMatch(html, /<input|<textarea|<script>/)
  card.props.enabled = false
  assert.doesNotMatch(await card.render(), /<button/)
  await card.state.submit('confirm')
  assert.equal(card.calls.length, 0)
})

test('generic confirmations deduplicate input clicks, retain immutable suggestions, expire and cancel through the existing input channel', async (t) => {
  let finish
  const card = controls(t, { append: () => new Promise((resolve) => { finish = resolve }) })
  const first = card.state.submit('confirm')
  await card.state.submit('confirm')
  await card.state.submit('cancel')
  assert.equal(card.calls.length, 1)
  assert.deepEqual(card.calls[0][2], { request_id: ID, content: 'capability-confirm:{"token":"opaque_feedback_token_1234567890","action":"confirm"}' })
  finish({ turn_id: card.props.turnId, request_id: ID, status: 'pending' })
  await first
  assert.equal(card.state.state.value, 'submitted')
  assert.deepEqual(card.props.confirmation.suggested, proposal().suggested)
  const expired = controls(t)
  await expired.expire()
  await expired.state.submit('confirm')
  assert.equal(expired.calls.length, 0)
  expired.state.redescribe()
  assert.deepEqual(expired.emitted, ['沉淀为案例'])
  const cancelled = controls(t)
  cancelled.state.redescribe()
  await setImmediate()
  assert.match(cancelled.calls[0][2].content, /"action":"cancel"/)
})

test('candidate confirmation keeps the real change visible while folding record identifiers and versions', async (t) => {
  const changes = [{ asset: 'analysis_prompt', before_version: 'prompt-v1', after_version: 'prompt-v2', reason: '减少歧义' }]
  const card = controls(t, { props: { confirmation: proposal({ hook: 'propose_bundle_candidate', fields: [
    { label: '基准 Bundle ID', value: ID }, { label: '候选版本', value: 'conversation-technical-version' },
    { label: '实际配置差异', value: JSON.stringify(changes) }, { label: '建议理由', value: '减少歧义' },
    { label: '操作说明', value: '仅提交候选，评测和人工审批后再激活。' },
  ] }) } })
  const html = await card.render()
  const visible = html.replace(/<details\b[^>]*>[\s\S]*?<\/details>/g, '')
  assert.match(visible, /分析 Prompt：prompt-v1 → prompt-v2/)
  assert.match(visible, /仅提交候选，评测和人工审批后再激活/)
  assert.doesNotMatch(visible, /conversation-technical-version|基准 Bundle ID|有效至/)
  assert.match(html, /<details class="na-capability-confirmation__details"><summary>确认详情<\/summary>/)
  assert.match(html, /before_version/)
})

test('evaluation confirmation exposes dataset source counts and Fresh side effects while folding complete immutable lineage', async (t) => {
  const cohorts = Object.fromEntries(['golden', 'fresh_bad_case', 'high_risk_regression'].map((layer) => [layer,
    { dataset_id: ID, content_sha256: 'immutable-lineage-hash', case_count: 3, dataset_version: `${layer}-v1` }]))
  const card = controls(t, { props: { confirmation: proposal({ hook: 'start_data_loop', fields: [
    { label: '候选 ID', value: ID }, { label: '冻结数据集来源（三层）', value: JSON.stringify(cohorts) },
    { label: '门禁策略版本', value: 'gate-v1' },
    { label: '操作说明', value: '复用已冻结黄金集和高风险集；新冻结错误案例集，通过后仍需人工批准。' },
  ] }) } })
  const html = await card.render()
  const visible = html.replace(/<details\b[^>]*>[\s\S]*?<\/details>/g, '')
  assert.match(visible, /已冻结黄金集：3 例/)
  assert.match(visible, /错误案例来源集：3 例/)
  assert.match(visible, /已冻结高风险集：3 例/)
  assert.match(visible, /新冻结错误案例集，通过后仍需人工批准/)
  assert.match(visible, new RegExp(ID))
  assert.doesNotMatch(visible, /immutable-lineage-hash|gate-v1|content_sha256/)
  assert.match(html, /immutable-lineage-hash/)
})

test('historical evaluation field aliases render the same clear dataset and source-window information', async (t) => {
  const cohorts = Object.fromEntries(['golden', 'fresh_bad_case', 'high_risk_regression'].map((layer) => [layer,
    { dataset_id: ID, content_sha256: 'historical-lineage-hash', case_count: 2, dataset_version: `${layer}-v1` }]))
  const card = controls(t, { formats: load(source('utils/format.ts')), props: { confirmation: proposal({ hook: 'start_data_loop', fields: [
    { label: 'cohorts', value: JSON.stringify(cohorts) },
    { label: 'window_start', value: '2026-10-03T00:00:00Z' }, { label: 'window_end', value: '2026-10-03T01:00:00Z' },
  ] }) } })
  const html = await card.render()
  const visible = html.replace(/<details\b[^>]*>[\s\S]*?<\/details>/g, '')
  assert.match(visible, /冻结数据集来源（三层）/)
  assert.match(visible, /已冻结黄金集：2 例/)
  assert.match(visible, /错误案例来源集：2 例/)
  assert.match(visible, /已冻结高风险集：2 例/)
  assert.match(visible, /错误案例来源窗口起点/)
  assert.match(visible, /来源截止时间/)
  assert.doesNotMatch(visible, /cohorts|window_start|window_end|historical-lineage-hash|content_sha256|T00:00:00Z|T01:00:00Z/)
  assert.match(html, /historical-lineage-hash/)
  assert.match(html, /window_start原始记录/)
  assert.match(html, /2026-10-03T00:00:00Z/)
})

test('feedback links read the exact case independently of list pagination and fail truthfully for unavailable targets', async (t) => {
  const route = vue.reactive({ query: { case_id: ID } })
  const calls = []
  const labelReads = []
  const unmounts = []
  const api = { getFeedbackCase: async (id) => { calls.push(id); if (id === OTHER) throw new Error('case not found');
    return { case: { id, source_reference: {}, production_bundle_version: 'bundle-v1' } } },
    listFeedbackCases: async () => ({ cases: [] }), listLabels: async (id) => { labelReads.push(id); return { labels: [] } } }
  const Panel = component('views/data-loop/FeedbackCasesPanel.vue', {
    vue: { ...vue, onMounted() {}, onBeforeUnmount: (callback) => unmounts.push(callback) },
    'vue-router': { useRoute: () => route }, '@/api': { dataLoopApi: api },
    '@/components/NaJsonBlock.vue': { default: {} }, '@/components/NaStateBlock.vue': { default: {} },
    '@/composables/useAsyncTask': { useAsyncTask: () => ({ pending: vue.ref(false), run: (action) => action() }) },
    '@/stores/confirm': { useConfirmStore: () => ({ ask: async () => true }) },
    '@/stores/toast': { useToastStore: () => ({ warn() {} }) }, '@/utils/errors': { describeError: (error) => error.message },
    '@/utils/format': { ...formats, newIdempotencyKey: () => 'idempotent' }, '@/utils/labels': labels,
    '@/utils/operationsFeedback': { readOperationsFeedback: () => null },
    '@/utils/validate': load(source('utils/validate.ts')), '@/utils/capabilityReceipts': receipts,
  }, false)
  const scope = vue.effectScope()
  const state = scope.run(() => Panel.setup({}, { expose() {} }))
  t.after(() => { for (const unmount of unmounts) unmount(); scope.stop() })
  await setImmediate()
  assert.deepEqual(calls, [ID])
  assert.equal(state.selected.value.id, ID)
  assert.deepEqual(labelReads, [ID])
  await state.reload(100)
  assert.equal(state.cases.value.length, 0)
  assert.equal(state.selected.value.id, ID, 'direct case detail survives a list page that does not contain it')
  route.query = { case_id: OTHER }
  await setImmediate()
  assert.equal(state.selected.value, null)
  assert.equal(state.linkedError.value, 'case not found')
  route.query = { case_id: [ID, OTHER] }
  await vue.nextTick()
  assert.equal(calls.length, 2)
  assert.match(state.linkedError.value, /不合法/)
})

test('feedback detail API encodes its target and reuses existing authentication', async () => {
  const calls = []
  const api = load(source('api/dataLoop.ts'), { '@/api/http': { request: async (...arguments_) => { calls.push(arguments_) } } }).dataLoopApi
  await api.getFeedbackCase('case/id')
  assert.deepEqual(calls, [['/api/v1/data-loop/feedback-cases/case%2Fid']])
})
