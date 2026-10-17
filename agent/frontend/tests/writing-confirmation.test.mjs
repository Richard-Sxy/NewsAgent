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
  const { outputText } = ts.transpileModule(code, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  })
  const exports = {}
  new Function('require', 'exports', ...Object.keys(globals), outputText)((name) => {
    assert.ok(Object.hasOwn(imports, name), `Unexpected import ${name}`)
    return imports[name]
  }, exports, ...Object.values(globals))
  return exports
}
const admission = load(source('utils/admission.ts'))
const receiptUtils = load(source('utils/capabilityReceipts.ts'), { '@/utils/admission': admission })
const confirmationUtils = load(source('utils/writingConfirmation.ts'), { '@/utils/capabilityReceipts': receiptUtils, '@/utils/admission': admission })
const NOW = Date.parse('2026-10-12T03:00:00Z')
const INPUT_ID = '12345678-1234-5678-9012-123456789012'
const TURN_ID = '12345678-1234-5678-9012-123456789013'
const CONVERSATION_ID = '12345678-1234-5678-9012-123456789014'
const JOB_ID = '12345678-1234-5678-9012-123456789015'
const confirmation = (patch = {}) => ({ token: 'opaque_confirm_token_123456789012', hook: 'start_writing_intent',
  expires_at: '2026-10-12T03:01:00Z', input_request_id: INPUT_ID,
  suggested: { topic: 'AI 芯片周报', target_words: 1200, tone: 'neutral' }, state: 'pending', provenance: 'model_suggestion', ...patch })
const receipt = { entity_type: 'writing_job', entity_id: null, status: 'not_landed', version: null,
  deep_link: null, pending_gate: null, created_at: '2026-10-12T03:00:00Z' }
const trace = (patch = {}) => ({ name: 'start_writing_intent', status: 'completed', attempts: 1,
  arguments: {}, result: { confirmation: confirmation(), receipt }, error_code: null, ...patch })
class ApiError extends Error {
  constructor(status) { super('API error'); this.status = status }
}
function deferred() {
  let resolve
  let reject
  const promise = new Promise((accept, decline) => { resolve = accept; reject = decline })
  return { promise, resolve, reject }
}

function harness(t, options = {}) {
  const calls = []
  const emitted = []
  const timers = new Map()
  const unmounts = []
  let now = NOW
  let sequence = 0
  const props = vue.reactive({ confirmation: confirmation(), conversationId: CONVERSATION_ID, turnId: TURN_ID,
    active: true, enabled: true, outcome: null, inputSubmitted: false, originalContent: '写一篇 AI 芯片周报', ...options.props })
  const imports = {
    vue: { ...vue, onBeforeUnmount: (callback) => unmounts.push(callback) },
    '@/api/conversations': { conversationsApi: { appendInput: async (...arguments_) => {
      calls.push(arguments_)
      return options.append ? options.append(...arguments_) : { turn_id: TURN_ID, request_id: INPUT_ID, status: 'pending' }
    } } },
    '@/utils/writingConfirmation': confirmationUtils,
    '@/utils/format': { formatDateTime: (value) => value }, '@/api/http': { ApiError },
  }
  const globals = {
    Date: { now: () => now, parse: Date.parse },
    setTimeout: (callback, delay) => { timers.set(++sequence, { callback, delay }); return sequence },
    clearTimeout: (id) => timers.delete(id),
  }
  const descriptor = parse(source('components/conversation/WritingConfirmationCard.vue')).descriptor
  const Component = load(compileScript(descriptor, { id: 'confirmation-state-test' }).content, imports, globals).default
  const scope = vue.effectScope()
  const state = scope.run(() => Component.setup(props, { expose() {}, emit: (...arguments_) => emitted.push(arguments_) }))
  function unmount() { for (const callback of unmounts) callback(); scope.stop() }
  t.after(unmount)
  return { props, state, calls, emitted, timers, unmount,
    advance: async (value) => { now = value; const pending = [...timers.values()]; timers.clear(); for (const timer of pending) timer.callback(); await vue.nextTick() },
    render: async () => {
      const Card = load(compileScript(descriptor, { id: 'confirmation-render-test', inlineTemplate: true }).content, imports, globals).default
      return renderToString(vue.createSSRApp({ render: () => vue.h(Card, props) }))
    } }
}

test('confirmation projection accepts only deterministic completed hook snapshots with a valid unpersisted receipt', () => {
  assert.deepEqual(confirmationUtils.writingConfirmation(trace()), confirmation())
  for (const item of [trace({ name: 'model_text' }), trace({ status: 'failed' }), trace({ result: { assistant_content: JSON.stringify(confirmation()) } }),
    trace({ result: { confirmation: confirmation({ provenance: 'assistant_text' }), receipt } }),
    trace({ result: { confirmation: confirmation({ token: '<script>' }), receipt } }),
    trace({ result: { confirmation: confirmation({ input_request_id: 'not-uuid' }), receipt } }),
    trace({ result: { confirmation: confirmation({ suggested: { topic: '题目', target_words: Infinity, tone: 'neutral' } }), receipt } }),
    trace({ result: { confirmation: confirmation(), receipt: { ...receipt, entity_id: JOB_ID } } }),
  ]) assert.equal(confirmationUtils.writingConfirmation(item), null)
})

test('confirmation outcomes and pending input restoration disable old proposals from saved evidence', () => {
  assert.equal(confirmationUtils.confirmationOutcome([trace()]), null)
  assert.equal(confirmationUtils.confirmationOutcome([trace(), trace({ result: { workflow_accepted: true,
    receipt: { ...receipt, status: 'landed', entity_id: JOB_ID, version: '1', deep_link: `/jobs?job_id=${JOB_ID}` } } })]), 'started')
  assert.equal(confirmationUtils.confirmationOutcome([trace({ result: { confirmation_outcome: 'cancelled' } })]), 'cancelled')
  assert.equal(confirmationUtils.confirmationInput([{ request_id: INPUT_ID, status: 'pending' }], confirmation()), true)
  assert.equal(confirmationUtils.confirmationInput([{ request_id: INPUT_ID, status: 'applied' }], confirmation()), true)
  assert.equal(confirmationUtils.confirmationInput([{ request_id: INPUT_ID, status: 'failed' }], confirmation()), false)
  assert.equal(confirmationUtils.confirmationInput({ request_id: INPUT_ID }, confirmation()), false)
})

test('receipt panel keeps the saved proposal immutable and shows the later accepted job as a separate card', async (t) => {
  const props = vue.reactive({ tools: [trace()], conversationId: CONVERSATION_ID, turnId: TURN_ID, active: true,
    writingEnabled: true, inputs: [], originalContent: '写一篇 AI 芯片周报' })
  const Panel = load(compileScript(parse(source('components/conversation/CapabilityReceiptsPanel.vue')).descriptor,
    { id: 'receipt-proposal-test' }).content, {
    vue, '@/utils/capabilityReceipts': receiptUtils, '@/utils/writingConfirmation': confirmationUtils,
    '@/components/conversation/CapabilityReceiptCard.vue': { default: {} },
    '@/components/conversation/WritingJobCard.vue': { default: {} },
    '@/components/conversation/WritingConfirmationCard.vue': { default: {} },
    '@/components/conversation/MemoryCard.vue': { default: {} },
    '@/components/conversation/FeedbackCaseCard.vue': { default: {} },
    '@/components/conversation/EvaluationRunCard.vue': { default: {} },
    '@/components/conversation/CapabilityConfirmationCard.vue': { default: {} },
    '@/utils/capabilityConfirmation': { capabilityConfirmation: () => null, capabilityConfirmationInput: () => false,
      capabilityConfirmationOutcome: () => null },
  }).default
  const scope = vue.effectScope()
  t.after(() => scope.stop())
  const state = scope.run(() => Panel.setup(props, { expose() {}, emit() {} }))
  assert.equal(state.proposals.value.length, 1)
  assert.equal(state.receipts.value.length, 0)
  assert.equal(state.outcome.value, null)
  props.tools.push(trace({ result: { workflow_accepted: true, receipt: { ...receipt, status: 'landed', entity_id: JOB_ID,
    version: '1', deep_link: `/jobs?job_id=${JOB_ID}` }, job: { id: JOB_ID, topic: 'AI 芯片周报', status: 'created',
    current_step: null, progress_percent: 0, created_at: '2026-10-12T03:00:10Z', updated_at: '2026-10-12T03:00:10Z' } } }))
  await vue.nextTick()
  assert.equal(state.proposals.value.length, 1)
  assert.equal(state.outcome.value, 'started')
  assert.equal(state.receipts.value.length, 1)
  assert.equal(state.receipts.value[0].job.id, JOB_ID)
  assert.equal(props.tools[0].result.receipt.status, 'not_landed')
  assert.equal(props.tools[0].result.confirmation.state, 'pending')
})

test('confirmation card actually renders immutable model suggestions as escaped text and hides action controls without permission', async (t) => {
  const card = harness(t, { props: { confirmation: confirmation({ suggested: {
    topic: '<img src=x onerror=alert(1)>AI 芯片周报', target_words: 1200, tone: 'explanatory',
  } }) } })
  let html = await card.render()
  assert.match(html, /待确认 · 模型建议/)
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/)
  assert.match(html, /1200 字/)
  assert.match(html, /解释说明/)
  assert.match(html, /确认启动/)
  assert.doesNotMatch(html, /<input|<select|<textarea|opaque_confirm_token/)
  card.props.enabled = false
  html = await card.render()
  assert.doesNotMatch(html, /<button/)
  await card.state.submit('confirm')
  assert.equal(card.calls.length, 0)
})

test('double clicks produce one input submission with the fixed request UUID and structured token', async (t) => {
  const pending = deferred()
  const card = harness(t, { append: () => pending.promise })
  const first = card.state.submit('confirm')
  await card.state.submit('confirm')
  await card.state.submit('cancel')
  assert.equal(card.calls.length, 1)
  assert.deepEqual(card.calls[0], [CONVERSATION_ID, TURN_ID, { request_id: INPUT_ID,
    content: 'capability-confirm:{"token":"opaque_confirm_token_123456789012","action":"confirm"}' }])
  assert.equal(card.state.state.value, 'submitting')
  pending.resolve({ turn_id: TURN_ID, request_id: INPUT_ID, status: 'pending' })
  await first
  assert.equal(card.state.state.value, 'submitted')
  await card.state.submit('confirm')
  assert.equal(card.calls.length, 1)
  assert.equal(card.emitted.length, 0)
})

test('transport uncertainty retries the same action and ID while terminal/permission rejection invalidates confirmation', async (t) => {
  let requests = 0
  const card = harness(t, { append: async () => {
    if (++requests === 1) throw new TypeError('network lost')
    return { turn_id: TURN_ID, request_id: INPUT_ID, status: 'pending' }
  } })
  await card.state.submit('confirm')
  assert.equal(card.state.state.value, 'retryable')
  await card.state.submit('cancel')
  assert.equal(card.calls.length, 1)
  await card.state.submit('confirm')
  assert.equal(card.calls.length, 2)
  assert.deepEqual(card.calls[0], card.calls[1])
  for (const status of [403, 404, 409, 422]) {
    const rejected = harness(t, { append: async () => { throw new ApiError(status) } })
    await rejected.state.submit('confirm')
    assert.equal(rejected.state.state.value, 'invalid')
    await rejected.state.submit('confirm')
    assert.equal(rejected.calls.length, 1)
  }
})

test('expiry timers revoke confirmation, terminal outcomes prevent execution, and re-describe only restores a draft', async (t) => {
  const card = harness(t)
  assert.equal(card.timers.size, 1)
  await card.advance(NOW + 60_001)
  assert.equal(card.state.expired.value, true)
  await card.state.submit('confirm')
  assert.equal(card.calls.length, 0)
  card.state.redescribe()
  assert.deepEqual(card.emitted, [['redescribe', '写一篇 AI 芯片周报']])
  for (const props of [{ active: false }, { outcome: 'started' }, { outcome: 'cancelled' }, { inputSubmitted: true }]) {
    const inactive = harness(t, { props })
    await inactive.state.submit('confirm')
    assert.equal(inactive.calls.length, 0)
  }
})

test('active re-describe first cancels using the same input channel, then restores the original draft without a new turn', async (t) => {
  const card = harness(t)
  card.state.redescribe()
  await setImmediate()
  assert.equal(card.calls.length, 1)
  assert.match(card.calls[0][2].content, /"action":"cancel"/)
  assert.deepEqual(card.emitted, [['redescribe', '写一篇 AI 芯片周报']])
  assert.equal(card.state.state.value, 'submitted')
  await card.state.submit('confirm')
  assert.equal(card.calls.length, 1)
})

test('component unmount clears expiry timer and ignores a late input response after conversation navigation', async (t) => {
  const pending = deferred()
  const card = harness(t, { append: () => pending.promise })
  const operation = card.state.submit('cancel')
  card.unmount()
  assert.equal(card.timers.size, 0)
  pending.resolve({ turn_id: TURN_ID, request_id: INPUT_ID, status: 'pending' })
  await operation
  assert.equal(card.emitted.length, 0)
})

test('confirmation API reuses the existing authenticated append-input endpoint without passing identity or suggestion edits', async () => {
  const calls = []
  const api = load(source('api/conversations.ts'), { '@/api/http': { request: async (...arguments_) => { calls.push(arguments_) } },
    '@/api/conversationStream': { streamConversationMessage() {} } }).conversationsApi
  await api.appendInput('conversation/id', 'turn/id', { request_id: INPUT_ID, content: 'capability-confirm:{"token":"opaque","action":"confirm"}' })
  assert.deepEqual(calls, [['/api/v1/conversations/conversation%2Fid/turns/turn%2Fid/inputs', { method: 'POST',
    body: { request_id: INPUT_ID, content: 'capability-confirm:{"token":"opaque","action":"confirm"}' } }]])
})
