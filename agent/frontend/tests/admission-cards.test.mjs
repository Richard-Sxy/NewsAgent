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
const labels = load(source('utils/labels.ts'))
const format = load(source('utils/format.ts'))
const writing = load(source('utils/writingConfirmation.ts'), { '@/utils/capabilityReceipts': receipts, '@/utils/admission': admission })
const generic = load(source('utils/capabilityConfirmation.ts'), { '@/utils/capabilityReceipts': receipts, '@/utils/admission': admission, '@/utils/labels': labels })
const polling = load(source('utils/entityPolling.ts'))
const JOB = '12345678-1234-5678-9012-123456789012'
const WORKFLOW = `hot-news-data-loop-${'a'.repeat(64)}`
const TICKET = `conversation-admission-request-${'b'.repeat(64)}`
const queue = (patch = {}) => ({ state: 'queued', queue_position: 2, active_count: 3, budget: 2,
  bootstrap_complete: true, error_code: null, request_workflow_id: TICKET, ...patch })
const unknownAdmission = () => queue({ state: 'unknown', queue_position: null, active_count: null,
  budget: null, bootstrap_complete: null, error_code: 'admission_snapshot_unavailable' })
const execution = (patch = {}) => admission.executionSnapshot({ admission: queue(), workflow_accepted: false, admission_accepted: true, ...patch })
const receipt = (entity_type, patch = {}) => ({ entity_type, entity_id: entity_type === 'writing_job' ? JOB : WORKFLOW,
  status: 'landed', version: '1', deep_link: entity_type === 'writing_job' ? `/jobs?job_id=${JOB}` : `/data-loop?workflow_id=${WORKFLOW}`,
  pending_gate: null, created_at: '2026-10-13T00:00:00Z', ...patch })
const queuedReceipt = () => receipt('evaluation_run', { status: 'not_landed', entity_id: null, version: null, deep_link: null })
const job = (patch = {}) => ({ id: JOB, topic: 'AI 芯片周报', status: 'created', current_step: null, progress_percent: 0,
  created_at: '2026-10-13T00:00:00Z', updated_at: '2026-10-13T00:00:00Z', ...patch })
const run = (patch = {}) => ({ workflow_id: WORKFLOW, phase: 'queued', gate_passed: null, waiting_for_approval: false,
  evaluation_run_id: null, dataset_id: null, policy_version: 'approved-policy-v1', terminal: false, ...patch })
const trace = (name, result) => ({ name, status: 'completed', attempts: 1, arguments: {}, result, error_code: null })
const RouterLink = (props, { slots }) => vue.h('a', { href: props.to }, slots.default?.())
RouterLink.props = ['to']
const component = (path, imports) => load(compileScript(parse(source(path)).descriptor, { id: path, inlineTemplate: true }).content, { vue, ...imports }).default
const BaseCard = component('components/conversation/CapabilityReceiptCard.vue', {
  'vue-router': { RouterLink }, '@/utils/capabilityReceipts': receipts, '@/utils/format': format,
})
const AdmissionStatus = component('components/conversation/AdmissionStatus.vue', { '@/utils/admission': admission })
const render = (Card, props) => renderToString(vue.createSSRApp({ render: () => vue.h(Card, props) }))

test('admission projection retains authoritative queue integers, including imported active jobs above the budget', () => {
  assert.deepEqual(admission.parseAdmission(queue()), queue())
  assert.equal(admission.parseAdmission(queue({ active_count: null, bootstrap_complete: false })).active_count, null)
  for (const patch of [{ queue_position: 0 }, { active_count: -1 }, { budget: Infinity }, { request_workflow_id: 'model-invented-id' },
    { active_count: Number.MAX_SAFE_INTEGER + 1 }]) assert.equal(admission.parseAdmission(queue(patch)), null)
  assert.equal(admission.executionLabel(execution()), '排队中')
  assert.equal(admission.executionLabel(execution({ admission: queue({ state: 'granted', queue_position: null }) })), '已取得并发名额，等待启动')
})

test('writing job cards render a real stored job in queue without asserting business Workflow start', async () => {
  const Card = component('components/conversation/WritingJobCard.vue', {
    '@/components/conversation/CapabilityReceiptCard.vue': { default: BaseCard }, '@/components/conversation/AdmissionStatus.vue': { default: AdmissionStatus },
    '@/composables/useWritingJobPolling': { useWritingJobPolling: () => ({ state: vue.ref({ value: null, loading: false, degraded: false, error: null }), refresh() {} }) },
    '@/utils/capabilityReceipts': receipts, '@/utils/format': format, '@/utils/labels': labels,
  })
  const html = await render(Card, { receipt: receipt('writing_job'), snapshot: job(), execution: execution() })
  assert.match(html, /已落库/)
  assert.match(html, /排队中/)
  assert.match(html, /队列位置：2/)
  assert.match(html, /租户活跃任务：3 \/ 并发预算：2/)
  assert.match(html, /<details><summary>排队详情<\/summary>/)
  assert.match(html, new RegExp(`href="/jobs\\?job_id=${JOB}"`))
  assert.doesNotMatch(html, /Workflow 已启动|已确认启动/)
})

test('queued evaluation requests preserve gray all-null receipts and expose no business link or fictitious evaluation row', async () => {
  const Card = component('components/conversation/EvaluationRunCard.vue', {
    'vue-router': { RouterLink }, '@/components/conversation/CapabilityReceiptCard.vue': { default: BaseCard },
    '@/components/conversation/AdmissionStatus.vue': { default: AdmissionStatus }, '@/utils/capabilityReceipts': receipts,
    '@/composables/useEvaluationPolling': { useEvaluationPolling: () => ({ state: vue.ref({ value: null, loading: false, degraded: false, error: null }), refresh() {} }) },
  })
  const html = await render(Card, { receipt: queuedReceipt(), snapshot: run(), execution: execution() })
  assert.match(html, /na-receipt__badge--not_landed/)
  assert.match(html, /排队中/)
  assert.match(html, /评测行 ID：尚未生成/)
  assert.doesNotMatch(html, /href=|实体 ID|回执版本|Workflow 已受理|Workflow 已启动/)
})

test('an immutable queued evaluation card gains a live business link only after an authoritative accepted receipt', async () => {
  const original = queuedReceipt()
  const Card = component('components/conversation/EvaluationRunCard.vue', {
    'vue-router': { RouterLink }, '@/components/conversation/CapabilityReceiptCard.vue': { default: BaseCard },
    '@/components/conversation/AdmissionStatus.vue': { default: AdmissionStatus }, '@/utils/capabilityReceipts': receipts,
    '@/composables/useEvaluationPolling': { useEvaluationPolling: () => ({ state: vue.ref({ value: { run: run({ phase: 'evaluate_candidate' }),
      receipt: receipt('evaluation_run'), execution: execution({ workflow_accepted: true, admission: queue({ state: 'running', queue_position: null }) }) },
      loading: false, degraded: false, error: null }), refresh() {} }) },
  })
  const html = await render(Card, { receipt: original, snapshot: run(), execution: execution() })
  assert.match(html, /na-receipt__badge--not_landed/)
  assert.match(html, /当前最新状态[\s\S]*评测候选[\s\S]*<summary>当时快照<\/summary>[\s\S]*排队中/)
  assert.match(html, new RegExp(`href="/data-loop\\?workflow_id=${WORKFLOW}"`))
  assert.equal(original.entity_id, null)
  assert.equal(original.version, null)
})

test('only completed server traces with a valid admission acknowledgment may project a queued planned evaluation ID', () => {
  const result = { receipt: queuedReceipt(), run: run(), admission: queue(), admission_accepted: true, workflow_accepted: false }
  assert.equal(receipts.toolReceipts(trace('start_data_loop', result))[0].run.workflow_id, WORKFLOW)
  for (const patch of [{ admission_accepted: false }, { admission: { ...queue(), request_workflow_id: 'not-real' } }, { run: run({ workflow_id: 'invented' }) }]) {
    assert.equal(receipts.toolReceipts(trace('start_data_loop', { ...result, ...patch }))[0].run, null)
  }
  assert.deepEqual(receipts.toolReceipts({ ...trace('start_data_loop', result), status: 'failed' }), [])
  assert.equal(receipts.toolReceipts({ ...trace('start_data_loop', result), status: 'denied' })[0].run, null)
})

test('denied writing traces retain only a verified committed job after an authoritative admission rejection', () => {
  const result = { receipt: receipt('writing_job'), job: job({ version: '1' }),
    admission: queue({ state: 'rejected', queue_position: null, error_code: 'admission_queue_full' }),
    admission_accepted: false, workflow_accepted: false }
  const rejected = { ...trace('start_writing_intent', result), status: 'denied' }
  const projected = receipts.toolReceipts(rejected)
  assert.equal(projected.length, 1)
  assert.equal(projected[0].job.id, JOB)
  assert.equal(projected[0].execution.admission.error_code, 'admission_queue_full')
  for (const patch of [{ admission: queue() }, { admission: { ...result.admission, request_workflow_id: 'invented' } },
    { workflow_accepted: true }, { workflow_accepted: null }, { admission_accepted: true }, { admission_accepted: undefined },
    { job: job({ version: '2' }) }, { job: job({ version: '1', id: 'other-job' }) }, { job: job({ version: '1', progress_percent: Infinity }) },
    { receipt: receipt('writing_job', { deep_link: '/jobs' }) }, { receipt: receipt('writing_job', { deep_link: `/jobs?job_id=${JOB}&admin=1` }) },
    { receipt: receipt('writing_job', { status: 'pending_approval', pending_gate: 'research' }) }]) {
    assert.deepEqual(receipts.toolReceipts({ ...rejected, result: { ...result, ...patch } }), [])
  }
  assert.deepEqual(receipts.toolReceipts({ ...rejected, status: 'failed' }), [])
  assert.deepEqual(receipts.toolReceipts({ ...rejected, name: 'writing_job_status' }), [])
})

test('a rejected writing request keeps the saved job link and explains the refusal without a started claim', async () => {
  const Card = component('components/conversation/WritingJobCard.vue', {
    '@/components/conversation/CapabilityReceiptCard.vue': { default: BaseCard }, '@/components/conversation/AdmissionStatus.vue': { default: AdmissionStatus },
    '@/composables/useWritingJobPolling': { useWritingJobPolling: () => ({ state: vue.ref({ value: null, loading: false, degraded: false, error: null }), refresh() {} }) },
    '@/utils/capabilityReceipts': receipts, '@/utils/format': format, '@/utils/labels': labels,
  })
  const result = { receipt: receipt('writing_job'), job: job({ version: '1' }),
    admission: queue({ state: 'rejected', queue_position: null, error_code: 'admission_queue_full' }),
    admission_accepted: false, workflow_accepted: false }
  const item = receipts.toolReceipts({ ...trace('start_writing_intent', result), status: 'denied' })[0]
  const html = await render(Card, { receipt: item.receipt, snapshot: item.job, execution: item.execution })
  assert.match(html, /任务已保存，但启动请求已拒绝/)
  assert.match(html, /admission_queue_full/)
  assert.match(html, new RegExp(`href="/jobs\\?job_id=${JOB}"`))
  assert.doesNotMatch(html, /Workflow 已启动|排队中|队列位置/)
})

test('a denied evaluation admission shows the refusal without promoting its planned Workflow into a business entity', async () => {
  const Card = component('components/conversation/EvaluationRunCard.vue', {
    'vue-router': { RouterLink }, '@/components/conversation/CapabilityReceiptCard.vue': { default: BaseCard },
    '@/components/conversation/AdmissionStatus.vue': { default: AdmissionStatus }, '@/utils/capabilityReceipts': receipts,
    '@/composables/useEvaluationPolling': { useEvaluationPolling: () => ({ state: vue.ref({ value: null, loading: false, degraded: false, error: null }), refresh() {} }) },
  })
  const result = { receipt: queuedReceipt(), run: run({ phase: 'rejected' }),
    admission: queue({ state: 'rejected', queue_position: null, error_code: 'admission_queue_full' }),
    admission_accepted: false, workflow_accepted: false }
  const denied = { ...trace('start_data_loop', result), status: 'denied' }
  const item = receipts.toolReceipts(denied)[0]
  assert.equal(item.run, null)
  const html = await render(Card, { receipt: item.receipt, snapshot: item.run, execution: item.execution })
  assert.match(html, /评测启动请求已拒绝，任务尚未启动/)
  assert.match(html, /admission_queue_full/)
  assert.match(html, /na-receipt__badge--not_landed/)
  assert.doesNotMatch(html, /href=|实体 ID|评测行 ID|Workflow 已受理|Workflow 已启动|刷新状态/)
  assert.deepEqual(receipts.toolReceipts({ ...denied, status: 'failed' }), [])
})

test('canonical unknown admission snapshots preserve diagnostic codes and nullable budget without implying a start', async () => {
  const current = execution({ admission: queue({ state: 'unknown', queue_position: null, active_count: null,
    budget: null, bootstrap_complete: null, error_code: 'admission_snapshot_unavailable' }), workflow_accepted: null, admission_accepted: null })
  const html = await render(AdmissionStatus, { execution: current })
  assert.match(html, /启动状态待核对/)
  assert.match(html, /租户活跃任务：待核对 \/ 并发预算：待核对/)
  assert.match(html, /admission_snapshot_unavailable/)
  assert.doesNotMatch(html, /Workflow 已启动|排队中|队列位置/)
})

test('confirmation acceptance ends the user operation while keeping queue acceptance distinct from business start', () => {
  const writingResult = { receipt: receipt('writing_job'), job: job(), admission: queue(), admission_accepted: true, workflow_accepted: false }
  assert.equal(writing.confirmationOutcome([trace('start_writing_intent', writingResult)]), 'accepted')
  assert.equal(writing.confirmationOutcome([trace('start_writing_intent', { ...writingResult, workflow_accepted: true })]), 'started')
  assert.equal(writing.confirmationOutcome([{ ...trace('start_writing_intent', writingResult), status: 'failed' }]), null)
  const evaluation = { receipt: queuedReceipt(), run: run(), admission: queue(), admission_accepted: true, workflow_accepted: false }
  assert.equal(generic.capabilityConfirmationOutcome([trace('start_data_loop', evaluation)], 'start_data_loop'), 'accepted')
  assert.equal(generic.capabilityConfirmationOutcome([trace('start_data_loop', { ...evaluation, receipt: receipt('evaluation_run'), workflow_accepted: true })], 'start_data_loop'), 'completed')
})

function clock(interval) {
  let sequence = 0
  const timers = new Map()
  const listeners = new Set()
  return { timers, listeners, environment: { visible: () => true,
    onVisibility: (callback) => { listeners.add(callback); return () => listeners.delete(callback) },
    schedule: (callback, delay) => { assert.equal(delay, interval); timers.set(++sequence, callback); return sequence },
    cancel: (id) => timers.delete(id) }, tick: async () => {
    const pending = [...timers.values()]; timers.clear(); for (const callback of pending) callback(); await setImmediate()
  } }
}

test('writing card polling uses the new authenticated receipt route once per entity and observes queue progression', async (t) => {
  const timers = clock(3000)
  const unmounts = []
  const reads = []
  let started = false
  const module = load(source('composables/useWritingJobPolling.ts'), {
    vue: { ...vue, onBeforeUnmount: (callback) => unmounts.push(callback) }, '@/utils/capabilityReceipts': receipts, '@/utils/admission': admission,
    '@/utils/entityPolling': { createEntityPoller: (options) => polling.createEntityPoller({ ...options, environment: timers.environment }) },
    '@/api/conversations': { conversationsApi: { writingReceipt: async (id) => { reads.push(id); return { job: job({ status: started ? 'researching' : 'created' }),
      receipt: receipt('writing_job', { created_at: new Date().toISOString() }), workflow_accepted: started, admission_accepted: true,
      admission: queue(started ? { state: 'running', queue_position: null } : {}) } } } },
  })
  const scope = vue.effectScope()
  const saved = vue.ref(job())
  const args = () => [saved, vue.ref(null), vue.ref(execution()), vue.ref('2026-10-13T00:00:00Z')]
  const first = scope.run(() => module.useWritingJobPolling(...args()))
  const second = scope.run(() => module.useWritingJobPolling(...args()))
  t.after(() => { for (const unmount of unmounts) unmount(); scope.stop() })
  await setImmediate()
  assert.deepEqual(reads, [JOB])
  assert.equal(first.state.value.value.execution.workflowAccepted, false)
  assert.equal(second.state.value.value.execution.admission.queue_position, 2)
  started = true
  await timers.tick()
  assert.equal(first.state.value.value.execution.workflowAccepted, true)
  assert.equal(first.state.value.value.job.status, 'researching')
  saved.value = null
  await vue.nextTick()
  assert.equal(timers.timers.size, 0)
})

test('queued evaluation polling uses admission-aware receipts until business acceptance and stops at an admission terminal state', async (t) => {
  const timers = clock(15000)
  const unmounts = []
  let terminal = false
  const module = load(source('composables/useEvaluationPolling.ts'), {
    vue: { ...vue, onBeforeUnmount: (callback) => unmounts.push(callback) }, '@/utils/capabilityReceipts': receipts, '@/utils/admission': admission,
    '@/utils/entityPolling': { createEntityPoller: (options) => polling.createEntityPoller({ ...options, environment: timers.environment }) },
    '@/api/conversations': { conversationsApi: { evaluationReceipt: async () => ({ run: run({ phase: terminal ? 'rejected' : 'queued' }),
      receipt: queuedReceipt(), workflow_accepted: false, admission_accepted: true,
      admission: queue(terminal ? { state: 'rejected', queue_position: null } : {}), }) } },
  })
  const scope = vue.effectScope()
  const state = scope.run(() => module.useEvaluationPolling(vue.ref(run()), vue.ref('2026-10-12T00:00:00Z'), vue.ref(execution())))
  t.after(() => { for (const unmount of unmounts) unmount(); scope.stop() })
  await setImmediate()
  assert.equal(state.state.value.value.execution.workflowAccepted, false)
  assert.equal(timers.timers.size, 1)
  terminal = true
  await timers.tick()
  assert.equal(state.state.value.value.execution.admission.state, 'rejected')
  assert.equal(timers.timers.size, 0)
})

test('only the authenticated live read may retain a tenant-verified unknown planned evaluation without business acceptance', async (t) => {
  const timers = clock(15000)
  const unmounts = []
  const response = { run: run({ phase: 'unknown' }), receipt: queuedReceipt(), workflow_accepted: null,
    admission_accepted: null, admission: unknownAdmission() }
  const module = load(source('composables/useEvaluationPolling.ts'), {
    vue: { ...vue, onBeforeUnmount: (callback) => unmounts.push(callback) }, '@/utils/capabilityReceipts': receipts, '@/utils/admission': admission,
    '@/utils/entityPolling': { createEntityPoller: (options) => polling.createEntityPoller({ ...options, environment: timers.environment }) },
    '@/api/conversations': { conversationsApi: { evaluationReceipt: async () => response } },
  })
  const scope = vue.effectScope()
  const state = scope.run(() => module.useEvaluationPolling(vue.ref(run()), vue.ref('2026-10-12T00:00:00Z'), vue.ref(execution())))
  t.after(() => { for (const unmount of unmounts) unmount(); scope.stop() })
  await setImmediate()
  assert.equal(state.state.value.value.run.phase, 'unknown')
  assert.equal(state.state.value.value.execution.admission.error_code, 'admission_snapshot_unavailable')
  assert.equal(state.state.value.value.execution.workflowAccepted, null)
  assert.equal(state.state.value.error, null)
  assert.equal(state.state.value.degraded, false)
  assert.equal(timers.timers.size, 1)
  // An ordinary persisted tool trace still needs a positive admission acknowledgment.
  assert.equal(receipts.toolReceipts(trace('evaluation_status', response))[0].run, null)
})

test('live unknown reads reject mismatched IDs, missing acceptance flags, noncanonical admission, or non-null business fields', async () => {
  const response = { run: run({ phase: 'unknown' }), receipt: queuedReceipt(), workflow_accepted: null,
    admission_accepted: null, admission: unknownAdmission() }
  for (const patch of [{ run: run({ phase: 'unknown', workflow_id: `hot-news-data-loop-${'c'.repeat(64)}` }) },
    { run: run({ phase: 'queued' }) }, { run: run({ phase: 'unknown', gate_passed: true }) },
    { run: run({ phase: 'unknown', evaluation_run_id: JOB }) }, { run: run({ phase: 'unknown', terminal: true }) },
    { workflow_accepted: undefined }, { admission_accepted: undefined },
    { workflow_accepted: false }, { admission: { ...unknownAdmission(), request_workflow_id: 'invented' } },
    { admission: { ...unknownAdmission(), active_count: 1 } }, { admission: { ...unknownAdmission(), bootstrap_complete: true } },
    { receipt: { ...queuedReceipt(), version: 'made-up' } }, { receipt: { ...queuedReceipt(), entity_id: WORKFLOW } }]) {
    const timers = clock(15000)
    const unmounts = []
    const module = load(source('composables/useEvaluationPolling.ts'), {
      vue: { ...vue, onBeforeUnmount: (callback) => unmounts.push(callback) }, '@/utils/capabilityReceipts': receipts, '@/utils/admission': admission,
      '@/utils/entityPolling': { createEntityPoller: (options) => polling.createEntityPoller({ ...options, environment: timers.environment }) },
      '@/api/conversations': { conversationsApi: { evaluationReceipt: async () => ({ ...response, ...patch }) } },
    })
    const scope = vue.effectScope()
    try {
      const state = scope.run(() => module.useEvaluationPolling(vue.ref(run()), vue.ref('2026-10-12T00:00:00Z'), vue.ref(execution())))
      await setImmediate()
      assert.equal(state.state.value.value, null)
      assert.equal(state.state.value.error, '读取任务状态失败')
    } finally {
      for (const unmount of unmounts) unmount()
      scope.stop()
    }
  }
})

test('live unknown evaluation cards display the saved queue separately from safe diagnostics without a business link', async () => {
  const Card = component('components/conversation/EvaluationRunCard.vue', {
    'vue-router': { RouterLink }, '@/components/conversation/CapabilityReceiptCard.vue': { default: BaseCard },
    '@/components/conversation/AdmissionStatus.vue': { default: AdmissionStatus }, '@/utils/capabilityReceipts': receipts,
    '@/composables/useEvaluationPolling': { useEvaluationPolling: () => ({ state: vue.ref({ value: { run: run({ phase: 'unknown' }),
      receipt: queuedReceipt(), execution: execution({ admission: unknownAdmission(), workflow_accepted: null, admission_accepted: null }) },
      loading: false, degraded: false, error: null }), refresh() {} }) },
  })
  const html = await render(Card, { receipt: queuedReceipt(), snapshot: run(), execution: execution() })
  assert.match(html, /当前最新状态[\s\S]*启动状态待核对[\s\S]*<summary>当时快照<\/summary>[\s\S]*排队中/)
  assert.match(html, /admission_snapshot_unavailable/)
  assert.match(html, /运行阶段：unknown/)
  assert.match(html, /评测行 ID：尚未生成/)
  assert.doesNotMatch(html, /href=|实体 ID|Workflow 已受理|Workflow 已启动|连续读取失败/)
})

test('receipt APIs encode identifiers and send no browser identity, queue budget, or business-start mutation', async () => {
  const reads = []
  const api = load(source('api/conversations.ts'), { '@/api/http': { request: async (...arguments_) => { reads.push(arguments_) } },
    '@/api/conversationStream': { streamConversationMessage() {} } }).conversationsApi
  await api.writingReceipt('job/id')
  await api.evaluationReceipt('workflow/id')
  assert.deepEqual(reads, [['/api/v1/conversations/receipts/writing-jobs/job%2Fid'], ['/api/v1/conversations/receipts/evaluation-runs/workflow%2Fid']])
})
