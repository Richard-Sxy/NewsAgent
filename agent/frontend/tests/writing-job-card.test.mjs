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
  const { outputText } = ts.transpileModule(code, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  })
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
const format = load(source('utils/format.ts'))
const labels = load(source('utils/labels.ts'))
const JOB_ID = '12345678-1234-5678-9012-123456789012'
const OTHER_ID = '12345678-1234-5678-9012-123456789013'
const snapshot = (patch = {}) => ({ id: JOB_ID, topic: 'AI 芯片周报', status: 'researching', current_step: 'research',
  progress_percent: 10, created_at: '2026-10-12T00:00:00Z', updated_at: '2026-10-12T00:01:00Z', ...patch })
const receipt = (patch = {}) => ({ entity_type: 'writing_job', entity_id: JOB_ID, status: 'landed', version: '2',
  deep_link: `/jobs?job_id=${JOB_ID}`, pending_gate: null, created_at: '2026-10-12T00:01:00Z', ...patch })
const trace = (result, patch = {}) => ({ name: 'writing_job_status', status: 'completed', attempts: 1,
  arguments: {}, result, error_code: null, ...patch })

function component(path, imports) {
  const code = compileScript(parse(source(path)).descriptor, { id: path, inlineTemplate: true }).content
  return load(code, { vue, ...imports }).default
}
const RouterLink = (props, { slots }) => vue.h('a', { href: props.to }, slots.default?.())
RouterLink.props = ['to']
const BaseCard = component('components/conversation/CapabilityReceiptCard.vue', {
  'vue-router': { RouterLink }, '@/utils/capabilityReceipts': receipts, '@/utils/format': format,
})
const AdmissionStatus = component('components/conversation/AdmissionStatus.vue', { '@/utils/admission': admission })
const render = (card, props) => renderToString(vue.createSSRApp({ render: () => vue.h(card, props) }))

test('base receipt card actually renders all three states with IDs, version, timestamp and safe navigation', async () => {
  for (const [status, text] of [['landed', '已落库'], ['pending_approval', '待人工审批'], ['not_landed', '未落地']]) {
    const value = receipt(status === 'not_landed' ? { status, entity_id: null, version: null, deep_link: null } : { status })
    const html = await render(BaseCard, { receipt: value })
    assert.match(html, new RegExp(`na-receipt__badge--${status}`))
    assert.match(html, new RegExp(text))
    assert.match(html, /回执时间/)
    assert.match(html, /<details class="na-receipt__details"><summary>记录信息<\/summary>/)
    if (status === 'not_landed') assert.doesNotMatch(html, /href=|实体 ID|回执版本/)
    else {
      assert.match(html, new RegExp(JOB_ID))
      assert.match(html, /回执版本/)
      assert.match(html, new RegExp(`href="/jobs\\?job_id=${JOB_ID}"`))
    }
  }
})

test('unknown and malformed receipts render escaped read-only JSON without navigation or executable HTML', async () => {
  for (const value of [receipt({ entity_type: 'future_type', deep_link: 'javascript:alert(1)' }), { status: 'landed' }]) {
    const html = await render(BaseCard, { receipt: { ...value, injected: '<script>alert(1)</script>' } })
    assert.match(html, /只读数据/)
    assert.match(html, /&lt;script&gt;/)
    assert.doesNotMatch(html, /<script|href=|<button/)
  }
})

test('receipt links reject external routes, encoded traversal, backslashes and mismatched job IDs', async () => {
  for (const link of ['https://evil.invalid/jobs', '//evil.invalid/jobs', 'javascript:alert(1)',
    '/jobs/../jobs', '/%6aobs', '/jobs#approve', '/jobs?job_id=not-a-uuid', `/jobs?job_id=${OTHER_ID}`,
    `/jobs?job_id=${JOB_ID}\x7f`, `/jobs?job_id=%31${JOB_ID.slice(1)}`, '/jobs?',
    `/jobs?job_id=${JOB_ID}&job_id=${JOB_ID}`, `/jobs?job_id=${JOB_ID}&action=approve`,
    `/jobs?job_id=${JOB_ID}\\evil`, `/jobs?job_id=${JOB_ID}\n`]) {
    assert.equal(receipts.safeReceiptLink(receipt({ deep_link: link })), null)
    const html = await render(BaseCard, { receipt: receipt({ deep_link: link }) })
    assert.doesNotMatch(html, /href=/)
  }
})

test('all receipt entity routes and selectors exactly match the backend allowlist', () => {
  for (const [entity_type, path, selector] of [['hot_news_run', '/hot-news', 'run_id'], ['feedback_case', '/data-loop', 'case_id'],
    ['memory', '/data-loop', 'memory_id'], ['candidate', '/data-loop', 'candidate_id'], ['evaluation_run', '/data-loop', 'run_id']]) {
    const value = receipt({ entity_type, entity_id: 'saved-123', deep_link: `${path}?${selector}=saved-123` })
    assert.equal(receipts.safeReceiptLink(value), value.deep_link)
    assert.equal(receipts.safeReceiptLink({ ...value, deep_link: path }), path)
    for (const bad of [`${path}?${selector}=other`, `${path}?other_id=saved-123`, `${path}?${selector}=saved-123&action=approve`,
      `${path}?${selector}=%73aved-123`, '/jobs']) assert.equal(receipts.safeReceiptLink({ ...value, deep_link: bad }), null)
  }
  assert.equal(receipts.parseCapabilityReceipt(receipt({ pending_gate: 'research' })), null)
  assert.equal(receipts.parseCapabilityReceipt(receipt({ status: 'not_landed', entity_id: null, version: null,
    deep_link: null, pending_gate: 'research' })), null)
})

test('pending gates remain visible from persisted job state when Workflow snapshots are missing', () => {
  for (const [status, step, expected] of [['research_review', 'research', 'research'], ['outline_review', 'outline', 'outline'],
    ['final_review', 'finalize', 'final'], ['waiting_human', 'review', 'review'], ['waiting_human', null, 'human'],
    ['drafting', 'section_draft', null]]) assert.equal(receipts.writingJobGate(snapshot({ status, current_step: step })), expected)
})

test('receipt projection uses only completed tool snapshots and binds job data by receipt entity ID', () => {
  assert.deepEqual(receipts.toolReceipts(trace({ assistant_content: JSON.stringify(receipt()), job: snapshot() })), [])
  assert.deepEqual(receipts.toolReceipts(trace({ receipt: receipt(), job: snapshot() }, { status: 'failed' })), [])
  assert.deepEqual(receipts.toolReceipts(trace({ receipt: receipt(), job: snapshot() }, { status: 'denied' })), [])
  assert.equal(receipts.toolReceipts(trace({ receipt: receipt(), job: snapshot({ id: OTHER_ID }) }))[0].job, null)
  const items = receipts.toolReceipts(trace({ receipts: [receipt(), receipt({ entity_id: OTHER_ID, deep_link: `/jobs?job_id=${OTHER_ID}` })],
    items: [snapshot({ id: OTHER_ID, topic: '另一任务' }), snapshot()] }))
  assert.equal(items[0].job.topic, 'AI 芯片周报')
  assert.equal(items[1].job.topic, '另一任务')
  assert.equal(receipts.toolReceipts(trace({ receipt: receipt(), job: snapshot({ progress_percent: Infinity }) }))[0].job, null)
})

test('writing card renders immutable original snapshot and current state separately, with no approval action', async () => {
  const seed = snapshot({ status: 'research_review' })
  const value = receipt({ status: 'pending_approval', pending_gate: 'research' })
  const Card = component('components/conversation/WritingJobCard.vue', {
    '@/components/conversation/CapabilityReceiptCard.vue': { default: BaseCard },
    '@/components/conversation/AdmissionStatus.vue': { default: AdmissionStatus },
    '@/composables/useWritingJobPolling': { useWritingJobPolling: () => ({ state: vue.ref({ value: {
      job: snapshot({ status: 'drafting', current_step: 'section_draft', progress_percent: 60 }),
      pendingGate: null, workflowAvailable: true,
    }, loading: false, degraded: false, error: null }), refresh() {} }) },
    '@/utils/capabilityReceipts': receipts, '@/utils/format': format, '@/utils/labels': labels,
  })
  const html = await render(Card, { receipt: value, snapshot: seed })
  assert.match(html, /当前最新状态[\s\S]*撰写中[\s\S]*<details class="na-writing-card__snapshot na-writing-card__details"><summary>当时快照<\/summary>[\s\S]*资料审核/)
  assert.match(html, /等待人工审批：研究包/)
  assert.match(html, /回执版本[\s\S]*2/)
  assert.doesNotMatch(html, /确认启动|同意|批准|审批按钮/)
  assert.equal(seed.status, 'research_review')
  assert.equal(value.version, '2')
})

test('writing card renders an explicit unknown state and a refresh action after repeated read failures', async () => {
  const Card = component('components/conversation/WritingJobCard.vue', {
    '@/components/conversation/CapabilityReceiptCard.vue': { default: BaseCard },
    '@/components/conversation/AdmissionStatus.vue': { default: AdmissionStatus },
    '@/composables/useWritingJobPolling': { useWritingJobPolling: () => ({ state: vue.ref({ value: null,
      loading: false, degraded: true, error: '读取任务状态失败' }), refresh() {} }) },
    '@/utils/capabilityReceipts': receipts, '@/utils/format': format, '@/utils/labels': labels,
  })
  const html = await render(Card, { receipt: receipt(), snapshot: snapshot() })
  assert.match(html, /状态未知.*自动刷新已暂停/)
  assert.match(html, /<button[^>]*>刷新状态<\/button>/)
  assert.match(html, /href="\/jobs\?job_id=/)
})

function clock() {
  let visible = true
  let sequence = 0
  const timers = new Map()
  const listeners = new Set()
  return {
    timers, listeners,
    environment: {
      visible: () => visible,
      onVisibility: (callback) => { listeners.add(callback); return () => listeners.delete(callback) },
      schedule: (callback, delay) => { assert.equal(delay, 3000); timers.set(++sequence, callback); return sequence },
      cancel: (handle) => timers.delete(handle),
    },
    visibility: (value) => { visible = value; for (const callback of listeners) callback() },
    tick: async () => {
      const pending = [...timers.values()]
      timers.clear()
      for (const callback of pending) callback()
      await setImmediate()
    },
  }
}

function poller(read, timer) {
  return polling.createEntityPoller({ read, terminal: receipts.writingJobTerminal,
    newer: (candidate, current) => Date.parse(candidate.updated_at) > Date.parse(current.updated_at), environment: timer.environment })
}

test('polling is shared by entity, pauses when hidden, resumes when visible and cleans up on last unmount', async () => {
  const timer = clock()
  let calls = 0
  const manager = poller(async () => { calls++; return snapshot() }, timer)
  const states = [[], []]
  const first = manager.subscribe(JOB_ID, snapshot(), (state) => states[0].push(state))
  const second = manager.subscribe(JOB_ID, snapshot(), (state) => states[1].push(state))
  await setImmediate()
  assert.equal(calls, 1)
  assert.equal(timer.timers.size, 1)
  assert.equal(states[0].at(-1).value.status, 'researching')
  assert.equal(states[1].at(-1).value.status, 'researching')
  await timer.tick()
  assert.equal(calls, 2)
  timer.visibility(false)
  assert.equal(timer.timers.size, 0)
  await timer.tick()
  assert.equal(calls, 2)
  timer.visibility(true)
  await setImmediate()
  assert.equal(calls, 3)
  first.unsubscribe()
  assert.equal(timer.timers.size, 1)
  second.unsubscribe()
  assert.equal(timer.timers.size, 0)
  assert.equal(timer.listeners.size, 0)
})

test('terminal snapshots do not restart automatic polling; a fresh page restores only unfinished histories', async () => {
  const timer = clock()
  let calls = 0
  const manager = poller(async () => { calls++; return snapshot({ status: 'research_completed' }) }, timer)
  for (const status of ['research_completed', 'final_approved', 'published', 'failed', 'cancelled']) {
    const entry = manager.subscribe(JOB_ID, snapshot({ status }), () => {})
    await setImmediate()
    assert.equal(calls, 0)
    assert.equal(timer.timers.size, 0)
    entry.unsubscribe()
  }
  const unfinished = manager.subscribe(JOB_ID, snapshot(), () => {})
  await setImmediate()
  assert.equal(calls, 1)
  assert.equal(timer.timers.size, 0)
  timer.visibility(false)
  timer.visibility(true)
  await timer.tick()
  assert.equal(calls, 1)
  unfinished.unsubscribe()
  const restored = manager.subscribe(JOB_ID, snapshot(), () => {})
  await setImmediate()
  assert.equal(calls, 2)
  restored.unsubscribe()
})

test('three consecutive failures downgrade and stop; manual refresh deduplicates and restores a successful poll', async () => {
  const timer = clock()
  let fails = true
  let calls = 0
  let current
  const manager = poller(async () => { calls++; if (fails) throw new Error('offline'); return snapshot() }, timer)
  const subscription = manager.subscribe(JOB_ID, snapshot(), (state) => { current = state })
  await setImmediate()
  await timer.tick()
  await timer.tick()
  assert.equal(calls, 3)
  assert.equal(current.degraded, true)
  assert.equal(timer.timers.size, 0)
  timer.visibility(false)
  timer.visibility(true)
  await timer.tick()
  assert.equal(calls, 3)
  fails = false
  await Promise.all([subscription.refresh(), subscription.refresh()])
  assert.equal(calls, 4)
  assert.equal(current.degraded, false)
  assert.equal(timer.timers.size, 1)
  subscription.unsubscribe()
})

test('late in-flight reads cannot notify unmounted cards or create orphan timers; remount shares the existing read', async () => {
  const timer = clock()
  let finish
  let calls = 0
  const manager = poller(() => { calls++; return new Promise((resolve) => { finish = resolve }) }, timer)
  const old = []
  const first = manager.subscribe(JOB_ID, snapshot(), (state) => old.push(state))
  await setImmediate()
  first.unsubscribe()
  const notifications = old.length
  let current
  const second = manager.subscribe(JOB_ID, snapshot(), (state) => { current = state })
  finish(snapshot({ status: 'published' }))
  await setImmediate()
  assert.equal(calls, 1)
  assert.equal(old.length, notifications)
  assert.equal(current.value.status, 'published')
  assert.equal(timer.timers.size, 0)
  second.unsubscribe()
  assert.equal(timer.listeners.size, 0)
})

test('a newer saved snapshot remains authoritative when an older in-flight response arrives', async () => {
  const timer = clock()
  let finish
  const manager = poller(() => new Promise((resolve) => { finish = resolve }), timer)
  const first = manager.subscribe(JOB_ID, snapshot(), () => {})
  await setImmediate()
  let current
  const second = manager.subscribe(JOB_ID, snapshot({ status: 'final_approved', updated_at: '2026-10-12T00:03:00Z' }),
    (state) => { current = state })
  finish(snapshot())
  await setImmediate()
  assert.equal(current.value.status, 'final_approved')
  assert.equal(timer.timers.size, 0)
  first.unsubscribe()
  second.unsubscribe()
})

test('JobsView opens valid deep links directly outside filters, rejects non-UUID or duplicate query IDs and clears on navigation', async (t) => {
  const route = vue.reactive({ query: { job_id: JOB_ID } })
  const opened = []
  const store = { selectedId: null, openJob: (id) => { store.selectedId = id; opened.push(id) }, select: (id) => { store.selectedId = id },
    disconnectStream() {}, fetchList: async () => {} }
  const code = compileScript(parse(source('views/JobsView.vue')).descriptor, { id: 'jobs-links' }).content
  const View = load(code, {
    vue: { ...vue, onMounted() {}, onBeforeUnmount() {} },
    'vue-router': { useRoute: () => route, useRouter: () => ({ replace: async ({ query }) => { route.query = query } }) },
    '@/stores/jobs': { useJobsStore: () => store }, '@/utils/capabilityReceipts': receipts,
    ...Object.fromEntries(['JobCreateCard', 'JobInspector', 'JobTable'].map((name) => [`@/components/jobs/${name}.vue`, { default: {} }])),
    '@/components/NaPaginator.vue': { default: {} }, '@/components/NaStateBlock.vue': { default: {} },
  }).default
  const scope = vue.effectScope()
  const state = scope.run(() => View.setup({}, { expose() {} }))
  t.after(() => scope.stop())
  assert.deepEqual(opened, [JOB_ID])
  assert.equal(state.waitingOnly.value, true, 'deep link must not depend on filtered list membership')
  route.query = { job_id: [JOB_ID, OTHER_ID] }
  await vue.nextTick()
  assert.equal(store.selectedId, null)
  route.query = { job_id: '../evil' }
  await vue.nextTick()
  assert.equal(opened.length, 1)
  state.handleSelect(OTHER_ID)
  await vue.nextTick()
  assert.equal(store.selectedId, OTHER_ID)
  route.query = {}
  await vue.nextTick()
  assert.equal(store.selectedId, null)
})
