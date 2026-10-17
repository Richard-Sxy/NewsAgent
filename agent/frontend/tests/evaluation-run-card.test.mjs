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
function load(code, imports = {}) {
  const { outputText } = ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } })
  const exports = {}
  new Function('require', 'exports', outputText)((name) => {
    assert.ok(Object.hasOwn(imports, name), `Unexpected import ${name}`)
    return imports[name]
  }, exports)
  return exports
}
const admission = load(source('utils/admission.ts'))
const receipts = load(source('utils/capabilityReceipts.ts'), { '@/utils/admission': admission })
const polling = load(source('utils/entityPolling.ts'))
const labels = load(source('utils/labels.ts'))
const confirmations = load(source('utils/capabilityConfirmation.ts'), { '@/utils/capabilityReceipts': receipts, '@/utils/labels': labels, '@/utils/admission': admission })
const WORKFLOW = `hot-news-data-loop-${'a'.repeat(64)}`
const OTHER_WORKFLOW = `hot-news-data-loop-${'b'.repeat(64)}`
const ROW = '12345678-1234-5678-9012-123456789012'
const snapshot = (patch = {}) => ({ workflow_id: WORKFLOW, phase: 'created', gate_passed: null, waiting_for_approval: false,
  evaluation_run_id: null, dataset_id: null, policy_version: 'approved-policy-v1', terminal: false, ...patch })
const receipt = (patch = {}) => ({ entity_type: 'evaluation_run', entity_id: WORKFLOW, status: 'landed',
  version: 'approved-policy-v1', deep_link: `/data-loop?workflow_id=${WORKFLOW}`, pending_gate: null,
  created_at: '2026-10-13T00:00:00Z', ...patch })
const trace = (result, patch = {}) => ({ name: 'start_data_loop', status: 'completed', attempts: 1, arguments: {}, result, error_code: null, ...patch })
const RouterLink = (props, { slots }) => vue.h('a', { href: props.to }, slots.default?.())
RouterLink.props = ['to']
const component = (path, imports, inlineTemplate = true) => load(compileScript(parse(source(path)).descriptor,
  { id: path, inlineTemplate }).content, { vue, ...imports }).default
const BaseCard = component('components/conversation/CapabilityReceiptCard.vue', {
  'vue-router': { RouterLink }, '@/utils/capabilityReceipts': receipts, '@/utils/format': { formatDateTime: (value) => value },
})
const AdmissionStatus = component('components/conversation/AdmissionStatus.vue', { '@/utils/admission': admission })

test('evaluation receipts bind to the accepted Workflow ID and do not invent an evaluation row', async () => {
  const Card = component('components/conversation/EvaluationRunCard.vue', {
    'vue-router': { RouterLink }, '@/components/conversation/AdmissionStatus.vue': { default: AdmissionStatus },
    '@/components/conversation/CapabilityReceiptCard.vue': { default: BaseCard }, '@/utils/capabilityReceipts': receipts,
    '@/composables/useEvaluationPolling': { useEvaluationPolling: () => ({ state: vue.ref({ value: null, loading: false, degraded: false, error: null }), refresh() {} }) },
  })
  const html = await renderToString(vue.createSSRApp({ render: () => vue.h(Card, { receipt: receipt(), snapshot: snapshot() }) }))
  assert.match(html, /评测已受理/)
  assert.match(html, /评测行 ID：尚未生成/)
  assert.match(html, /approved-policy-v1/)
  assert.match(html, new RegExp(`href="/data-loop\\?workflow_id=${WORKFLOW}"`))
  assert.doesNotMatch(html, /批准|激活恢复|二审通过/)
  assert.equal(receipts.parseEvaluationSnapshot(snapshot({ workflow_id: OTHER_WORKFLOW }), WORKFLOW), null)
  assert.equal(receipts.parseEvaluationSnapshot(snapshot({ evaluation_run_id: 'not-a-row' }), WORKFLOW), null)
  assert.equal(receipts.parseEvaluationSnapshot(snapshot({ policy_version: 123 }), WORKFLOW, 'saved-policy'), null)
})

test('evaluation cards keep original snapshots apart from the latest gate and display a real row only after it exists', async () => {
  const Card = component('components/conversation/EvaluationRunCard.vue', {
    'vue-router': { RouterLink }, '@/components/conversation/AdmissionStatus.vue': { default: AdmissionStatus },
    '@/components/conversation/CapabilityReceiptCard.vue': { default: BaseCard }, '@/utils/capabilityReceipts': receipts,
    '@/composables/useEvaluationPolling': { useEvaluationPolling: () => ({ state: vue.ref({ value: { run: snapshot({ phase: 'waiting_approval',
      waiting_for_approval: true, gate_passed: true, evaluation_run_id: ROW, dataset_id: ROW }) }, loading: false, degraded: false, error: null }), refresh() {} }) },
  })
  const html = await renderToString(vue.createSSRApp({ render: () => vue.h(Card, { receipt: receipt(), snapshot: snapshot() }) }))
  assert.match(html, /当前最新状态[\s\S]*评测完成 · 待人工审批[\s\S]*<details class="na-evaluation-snapshot na-evaluation-details"><summary>当时快照<\/summary>[\s\S]*准备评测[\s\S]*评测行 ID：尚未生成/)
  assert.match(html, /待人工审批/)
  assert.match(html, new RegExp(`评测行 ID：${ROW}`))
})

test('canonical Workflow selectors reject injection and mismatched IDs while preserving historical run selectors', () => {
  assert.equal(receipts.safeReceiptLink(receipt()), `/data-loop?workflow_id=${WORKFLOW}`)
  for (const link of [`/data-loop?workflow_id=${OTHER_WORKFLOW}`, `/data-loop?workflow_id=${WORKFLOW}&action=approve`,
    '/data-loop?workflow_id=not-a-workflow', `/data-loop?workflow_id=%68${WORKFLOW.slice(1)}`, '/jobs']) {
    assert.equal(receipts.safeReceiptLink(receipt({ deep_link: link })), null)
  }
  assert.equal(receipts.safeReceiptLink(receipt({ deep_link: `/data-loop?run_id=${WORKFLOW}` })), null)
  assert.equal(receipts.safeReceiptLink(receipt({ entity_id: ROW, deep_link: `/data-loop?run_id=${ROW}` })), `/data-loop?run_id=${ROW}`)
  assert.equal(receipts.toolReceipts(trace({ receipt: receipt(), run: snapshot() }))[0].run.workflow_id, WORKFLOW)
})

test('snapshot API terminal phases are deterministic and preserve the policy from the saved receipt', () => {
  for (const phase of ['evaluation_failed', 'approval_expired', 'rejected', 'activated']) {
    const parsed = receipts.parseEvaluationSnapshot({ ...snapshot({ phase }), terminal: undefined, policy_version: undefined }, WORKFLOW, 'saved-policy')
    assert.equal(parsed.terminal, true)
    assert.equal(parsed.policy_version, 'saved-policy')
  }
  assert.equal(receipts.evaluationTerminal('waiting_approval'), false)
  const status = receipts.toolReceipts(trace({ receipt: receipt({ version: 'actual-snapshot-hash' }),
    run: { ...snapshot(), policy_version: undefined } }, { name: 'evaluation_status' }))[0].run
  assert.equal(status.policy_version, null, 'a snapshot hash must not be presented as an approved policy version')
  assert.equal(receipts.parseEvaluationSnapshot({ ...snapshot(), dataset_id: undefined }, WORKFLOW).dataset_id, null)
})

function fakeClock() {
  let visible = true
  let sequence = 0
  const timers = new Map()
  const listeners = new Set()
  return { timers, listeners, environment: {
    visible: () => visible,
    onVisibility: (callback) => { listeners.add(callback); return () => listeners.delete(callback) },
    schedule: (callback, delay) => { assert.equal(delay, 15000); timers.set(++sequence, callback); return sequence },
    cancel: (id) => timers.delete(id),
  }, visibility: (value) => { visible = value; for (const callback of listeners) callback() },
  tick: async () => { const pending = [...timers.values()]; timers.clear(); for (const callback of pending) callback(); await setImmediate() } }
}

test('evaluation polling shares one Workflow read, uses 15 seconds, pauses on hidden pages and stops on terminal snapshots', async () => {
  const clock = fakeClock()
  let calls = 0
  let terminal = false
  const manager = polling.createEntityPoller({ intervalMs: 15000, environment: clock.environment,
    read: async (_id, seed) => { calls++; assert.equal(seed.policy_version, 'approved-policy-v1'); return snapshot({ terminal }) },
    terminal: (value) => value.terminal, newer: () => false })
  const first = manager.subscribe(WORKFLOW, snapshot(), () => {})
  const second = manager.subscribe(WORKFLOW, snapshot(), () => {})
  await setImmediate()
  assert.equal(calls, 1)
  assert.equal(clock.timers.size, 1)
  clock.visibility(false)
  assert.equal(clock.timers.size, 0)
  clock.visibility(true)
  await setImmediate()
  assert.equal(calls, 2)
  terminal = true
  await clock.tick()
  assert.equal(calls, 3)
  assert.equal(clock.timers.size, 0)
  first.unsubscribe(); second.unsubscribe()
  assert.equal(clock.listeners.size, 0)
})

test('evaluation polling stops after three consecutive read failures and manual refresh resumes with the same Workflow', async () => {
  const clock = fakeClock()
  let failure = true
  let current
  const manager = polling.createEntityPoller({ intervalMs: 15000, environment: clock.environment,
    read: async () => { if (failure) throw new Error('offline'); return snapshot() }, terminal: (value) => value.terminal, newer: () => false })
  const subscription = manager.subscribe(WORKFLOW, snapshot(), (value) => { current = value })
  await setImmediate(); await clock.tick(); await clock.tick()
  assert.equal(current.degraded, true)
  assert.equal(clock.timers.size, 0)
  failure = false
  await subscription.refresh()
  assert.equal(current.degraded, false)
  assert.equal(clock.timers.size, 1)
  subscription.unsubscribe()
})

test('the evaluation composable reads the existing snapshot API, retains saved policy and cleans its shared subscription', async (t) => {
  const clock = fakeClock()
  const unmounts = []
  const reads = []
  const module = load(source('composables/useEvaluationPolling.ts'), {
    vue: { ...vue, onBeforeUnmount: (callback) => unmounts.push(callback) },
    '@/api/conversations': { conversationsApi: { evaluationReceipt: async (id) => { reads.push(id); return { receipt: receipt({ created_at: new Date().toISOString() }), run: {
      workflow_id: id, phase: 'waiting_approval', gate_passed: true, waiting_for_approval: true, evaluation_run_id: ROW, dataset_id: ROW,
    } } } } },
    '@/utils/entityPolling': { createEntityPoller: (options) => polling.createEntityPoller({ ...options, environment: clock.environment }) },
    '@/utils/capabilityReceipts': receipts,
    '@/utils/admission': admission,
  })
  const scope = vue.effectScope()
  const saved = vue.ref(snapshot())
  const state = scope.run(() => module.useEvaluationPolling(saved, vue.ref(new Date(Date.now() - 60000).toISOString()), vue.ref(undefined)))
  t.after(() => { for (const unmount of unmounts) unmount(); scope.stop() })
  await setImmediate()
  assert.deepEqual(reads, [WORKFLOW])
  assert.equal(state.state.value.value.run.policy_version, 'approved-policy-v1')
  assert.equal(state.state.value.value.run.evaluation_run_id, ROW)
  assert.equal(module.EVALUATION_POLL_INTERVAL_MS, 15000)
  saved.value = null
  await vue.nextTick()
  assert.equal(clock.timers.size, 0)
  assert.equal(clock.listeners.size, 0)
})

test('Data Loop confirmation suggestions permit only a candidate UUID; Python fields carry datasets, window and approved policy', () => {
  const pending = receipt({ status: 'not_landed', entity_id: null, version: null, deep_link: null })
  const confirmation = { hook: 'start_data_loop', token: 'opaque_dataloop_token_1234567890', expires_at: '2026-10-13T00:01:00Z', input_request_id: ROW,
    state: 'pending', provenance: 'model_suggestion', suggested: { candidate_id: ROW }, fields: [
      { label: '黄金集', value: 'golden-saved' }, { label: '高风险集', value: 'risk-saved' }, { label: '策略版本', value: 'approved-policy-v1' }] }
  assert.equal(confirmations.capabilityConfirmation(trace({ receipt: pending, confirmation })).hook, 'start_data_loop')
  assert.equal(confirmations.capabilityConfirmation(trace({ receipt: pending, confirmation: { ...confirmation,
    suggested: { candidate_id: ROW, policy_version: 'model-overridden' } } })), null)
  assert.equal(confirmations.capabilityConfirmationOutcome([trace({ receipt: receipt(), run: snapshot(), workflow_accepted: true })], 'start_data_loop'), 'completed')
  assert.equal(confirmations.capabilityConfirmationOutcome([trace({ receipt: receipt() }, { name: 'save_feedback_case' })], 'start_data_loop'), null)
})

test('candidate confirmations accept only server-admitted model/prompt versions and refuse model specs or unassessable reranker changes', () => {
  const pending = receipt({ entity_type: 'candidate', status: 'not_landed', entity_id: null, version: null, deep_link: null })
  const confirmation = { hook: 'propose_bundle_candidate', token: 'opaque_candidate_token_1234567890', expires_at: '2026-10-13T00:01:00Z', input_request_id: ROW,
    state: 'pending', provenance: 'model_suggestion', suggested: { parameter: 'analysis_prompt_version', value: 'approved-prompt-v2', reason: '改善解释' },
    fields: [{ label: '基线 Bundle', value: 'active-v1' }, { label: '实际差异', value: 'prompt-v1 → approved-prompt-v2' }] }
  assert.equal(confirmations.capabilityConfirmation(trace({ receipt: pending, confirmation }, { name: 'propose_bundle_candidate' })).hook, 'propose_bundle_candidate')
  for (const suggested of [{ parameter: 'vector_weight', value: 0.5, reason: '改重排' }, { parameter: 'unknown', value: 'v2', reason: '改配置' },
    { parameter: 'model_version', value: Infinity, reason: '改模型' }, { ...confirmation.suggested, spec: { active: true } }]) {
    assert.equal(confirmations.capabilityConfirmation(trace({ receipt: pending, confirmation: { ...confirmation, suggested } }, { name: 'propose_bundle_candidate' })), null)
  }
})

test('safe denied capability outcomes show only validated unpersisted receipts and a bounded text reason', () => {
  const pending = receipt({ entity_type: 'candidate', status: 'not_landed', entity_id: null, version: null, deep_link: null })
  const result = { receipt: pending, reason: '<script>unsupported</script>' }
  assert.equal(receipts.toolReceipts(trace(result, { status: 'denied' }))[0].reason, result.reason)
  assert.deepEqual(receipts.toolReceipts(trace(result, { status: 'failed' })), [])
  assert.deepEqual(receipts.toolReceipts(trace({ receipt: receipt(), reason: 'fake persisted result' }, { status: 'denied' })), [])
  assert.deepEqual(receipts.toolReceipts(trace({ receipt: { ...pending, entity_type: 'unknown' }, reason: 'unsafe' }, { status: 'denied' })), [])
})

test('Data Loop deep links route to the actual matching run or candidate panel and preserve selectors', () => {
  const routerModule = load(source('router/index.ts').replaceAll('import.meta.env.BASE_URL', "'/'"), { 'vue-router': {
    createWebHistory: () => ({}), createRouter: (configuration) => ({ ...configuration, afterEach() {} }),
  } })
  const route = routerModule.router.routes.find((item) => item.path === '/data-loop')
  assert.deepEqual(route.redirect({ query: { workflow_id: WORKFLOW } }), { name: 'data-loop-runs', query: { workflow_id: WORKFLOW } })
  assert.deepEqual(route.redirect({ query: { candidate_id: ROW } }), { name: 'data-loop-candidates', query: { candidate_id: ROW } })
})

function deferred() {
  let resolve
  const promise = new Promise((accept) => { resolve = accept })
  return { promise, resolve }
}

test('run and candidate panels load real linked snapshots and reject late or mismatched results', async (t) => {
  for (const [path, selector, initialId, nextId, method, resultField] of [
    ['RunsPanel', 'workflow_id', WORKFLOW, OTHER_WORKFLOW, 'snapshot', 'snapshot'],
    ['CandidatesPanel', 'candidate_id', ROW, '12345678-1234-5678-9012-123456789013', 'getCandidate', 'lookupResult'],
  ]) {
    const route = vue.reactive({ query: { [selector]: initialId } })
    const first = deferred()
    const reads = []
    const unmounts = []
    const response = (id) => method === 'snapshot' ? snapshot({ workflow_id: id }) : { candidate: { id, status: 'pending_evaluation' } }
    const api = { [method]: async (id) => { reads.push(id); return id === initialId ? first.promise : response(id) } }
    const Panel = component(`views/data-loop/${path}.vue`, {
      vue: { ...vue, onBeforeUnmount: (callback) => unmounts.push(callback) }, 'vue-router': { useRoute: () => route },
      '@/api': { dataLoopApi: api }, '@/components/NaJsonBlock.vue': { default: {} }, '@/components/NaStateBlock.vue': { default: {} },
      '@/composables/useAsyncTask': { useAsyncTask: () => ({ pending: vue.ref(false), run: (action) => action() }) },
      '@/stores/confirm': { useConfirmStore: () => ({ ask: async () => true }) }, '@/stores/toast': { useToastStore: () => ({ warn() {} }) },
      '@/utils/errors': { describeError: (error) => error.message }, '@/utils/format': { newIdempotencyKey: () => 'idempotent' },
      '@/utils/capabilityReceipts': receipts, '@/utils/validate': load(source('utils/validate.ts')),
    }, false)
    const scope = vue.effectScope()
    const state = scope.run(() => Panel.setup({}, { expose() {} }))
    t.after(() => { for (const unmount of unmounts) unmount(); scope.stop() })
    assert.deepEqual(reads, [initialId])
    route.query = { [selector]: nextId }
    await setImmediate()
    const current = state[resultField].value
    assert.equal(method === 'snapshot' ? current.workflow_id : current.candidate.id, nextId)
    first.resolve(response(initialId))
    await setImmediate()
    assert.equal(method === 'snapshot' ? state[resultField].value.workflow_id : state[resultField].value.candidate.id, nextId)
    route.query = { [selector]: [initialId, nextId] }
    await vue.nextTick()
    assert.equal(state[resultField].value, null)
    assert.equal(reads.length, 2)
  }
})
