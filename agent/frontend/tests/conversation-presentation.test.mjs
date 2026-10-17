import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

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
const labels = load(source('utils/labels.ts'))
const writing = load(source('utils/writingConfirmation.ts'), {
  '@/utils/capabilityReceipts': receipts, '@/utils/admission': admission,
})
const confirmation = load(source('utils/capabilityConfirmation.ts'), {
  '@/utils/capabilityReceipts': receipts, '@/utils/admission': admission, '@/utils/labels': labels,
})
const { preferCapabilityCards } = load(source('utils/conversationPresentation.ts'), {
  '@/utils/capabilityReceipts': receipts, '@/utils/writingConfirmation': writing,
  '@/utils/capabilityConfirmation': confirmation,
})

const ID = '12345678-1234-5678-9012-123456789012'
const OTHER = '12345678-1234-5678-9012-123456789013'
const WORKFLOW = `hot-news-data-loop-${'a'.repeat(64)}`
const STAMP = '2026-10-13T00:00:00Z'
const receipt = (entity_type, patch = {}) => ({ entity_type, entity_id: ID, version: '1', status: 'landed',
  deep_link: null, pending_gate: null, created_at: STAMP, ...patch })
const pending = (entity_type) => receipt(entity_type, { entity_id: null, version: null, status: 'not_landed' })
const trace = (name, result, patch = {}) => ({ name, result, status: 'completed', attempts: 1,
  arguments: {}, error_code: null, ...patch })
const turn = (tools, patch = {}) => ({ status: 'completed', error_code: null, tools, ...patch })
const job = (patch = {}) => ({ id: ID, topic: '科技周报', status: 'researching', current_step: 'research', progress_percent: 15,
  created_at: STAMP, updated_at: STAMP, ...patch })
const memory = (patch = {}) => ({ id: ID, content: '关注科技', tier: 'short_term', status: 'active', candidate_status: null,
  version: '1', expires_at: STAMP, not_promoted: false, ...patch })
const feedback = (patch = {}) => ({ case_id: ID, run_id: 'saved-run', news_id: 'saved-news', production_bundle_version: 'bundle-v1',
  issue_type: 'analysis_incorrect', expected_label_draft: '需核对事实', status: 'needs_label', ...patch })
const run = (patch = {}) => ({ workflow_id: WORKFLOW, phase: 'waiting_approval', gate_passed: true, waiting_for_approval: true,
  evaluation_run_id: ID, dataset_id: OTHER, policy_version: 'gate-v1', terminal: false, ...patch })
const proposal = (hook, suggested, fields) => ({ token: 'opaque_confirmation_1234567890', hook, suggested,
  fields, expires_at: STAMP, input_request_id: ID, state: 'pending', provenance: 'model_suggestion' })
const writingTrace = () => trace('writing_job_status', { receipt: receipt('writing_job'), job: job() })

test('only complete saved capability cards can replace duplicate prose on a successful turn', () => {
  const tools = [writingTrace(), trace('memory_status', { receipt: receipt('memory'), memory: memory() }),
    trace('save_feedback_case', { receipt: receipt('feedback_case'), feedback: feedback() }),
    trace('evaluation_status', { receipt: receipt('evaluation_run', { entity_id: WORKFLOW }), run: run() })]
  for (const tool of tools) assert.equal(preferCapabilityCards(turn([tool])), true, tool.name)
  assert.equal(preferCapabilityCards(turn(tools)), true)
  assert.equal(preferCapabilityCards(turn([trace('list_writing_jobs', { receipts: [receipt('writing_job')], items: [job()] })])), true)
})

test('failure, interruption, denied, mixed news, unknown hooks and empty results keep the full answer visible', () => {
  for (const value of [turn([]), turn([writingTrace()], { status: 'processing' }),
    turn([writingTrace()], { status: 'failed' }), turn([writingTrace()], { error_code: 'interrupted' }),
    turn([trace('writing_job_status', {}, { status: 'failed' })]),
    turn([trace('writing_job_status', { receipt: receipt('writing_job'), job: job() }, { status: 'denied' })]),
    turn([trace('writing_job_status', { receipt: receipt('writing_job'), job: job() }, { error_code: 'unknown' })]),
    turn([trace('writing_job_status', { receipt: receipt('writing_job'), job: job({ status: 'failed' }) })]),
    turn([trace('evaluation_status', { receipt: receipt('evaluation_run', { entity_id: WORKFLOW }), run: run({ phase: 'evaluation_failed' }) })]),
    turn([writingTrace(), trace('read_hot_news', { items: [{ title: 'news' }] })]),
    turn([writingTrace(), trace('future_hook', {})]),
    turn([trace('list_writing_jobs', { receipts: [], items: [] })])]) {
    assert.equal(preferCapabilityCards(value), false)
  }
})

test('bound receipts without complete matching snapshots, or with malformed siblings, never hide prose', () => {
  const cases = [trace('writing_job_status', { receipt: receipt('writing_job'), job: job({ id: OTHER }) }),
    trace('writing_job_status', { receipt: receipt('writing_job'), job: job({ progress_percent: Infinity }) }),
    trace('memory_status', { receipt: receipt('memory'), memory: memory({ id: OTHER }) }),
    trace('save_feedback_case', { receipt: receipt('feedback_case'), feedback: feedback({ case_id: OTHER }) }),
    trace('evaluation_status', { receipt: receipt('evaluation_run', { entity_id: WORKFLOW }), run: run({ workflow_id: `${WORKFLOW}b` }) }),
    trace('evaluation_status', { receipt: receipt('evaluation_run', { entity_id: WORKFLOW }), run: run({ phase: 'unknown' }) }),
    trace('evaluation_status', { receipt: receipt('evaluation_run', { entity_id: WORKFLOW }), run: run({ phase: 'future_phase' }) }),
    trace('memory_status', { receipt: receipt('memory'), memory: memory({ status: 'future_state' }) }),
    trace('save_feedback_case', { receipt: receipt('feedback_case'), feedback: feedback({ status: 'future_state' }) }),
    trace('writing_job_status', { receipt: receipt('memory'), memory: memory() }),
    trace('list_writing_jobs', { receipts: [receipt('writing_job'), receipt('future_entity')], items: [job()] }),
    trace('writing_job_status', { receipt: { ...receipt('writing_job'), deep_link: 'javascript:alert(1)' }, job: job() }),
    trace('writing_job_status', null)]
  for (const tool of cases) assert.equal(preferCapabilityCards(turn([tool])), false, tool.name)
})

test('writing and feedback confirmations use strict saved proposals; candidate prose stays visible', () => {
  const writingProposal = proposal('start_writing_intent', { topic: '科技周报', target_words: 800, tone: 'neutral' })
  const writingPending = trace('start_writing_intent', { receipt: pending('writing_job'), confirmation: writingProposal })
  const feedbackProposal = proposal('save_feedback_case', {
    run_id: 'saved-run', news_id: 'saved-news', issue_type: 'analysis_incorrect', expected_label_draft: '需核对事实',
  }, [{ label: '操作说明', value: '同时记录分析驳回；标签仅草稿' }])
  const feedbackPending = trace('save_feedback_case', { receipt: pending('feedback_case'), confirmation: feedbackProposal })
  const evaluationPending = trace('start_data_loop', { receipt: pending('evaluation_run'),
    confirmation: proposal('start_data_loop', { candidate_id: ID }, [{ label: '操作说明', value: '评测后待人工审批' }]) })
  for (const tool of [writingPending, feedbackPending, evaluationPending]) assert.equal(preferCapabilityCards(turn([tool])), true)
  assert.equal(preferCapabilityCards(turn([writingPending, trace('start_writing_intent', {
    receipt: receipt('writing_job'), job: job(), workflow_accepted: true,
  })])), true)
  assert.equal(preferCapabilityCards(turn([trace('start_writing_intent', { receipt: pending('writing_job'),
    confirmation: { ...writingProposal, provenance: 'model_text' } })])), false)
  assert.equal(preferCapabilityCards(turn([trace('save_feedback_case', { receipt: pending('feedback_case'),
    confirmation: { ...feedbackProposal, fields: [{ label: '操作说明', value: {} }] } })])), false)
  assert.equal(preferCapabilityCards(turn([trace('propose_bundle_candidate', { receipt: receipt('candidate'),
    candidate: { candidate_id: ID, status: 'pending_evaluation' } })])), false)
  assert.equal(preferCapabilityCards(turn([trace('propose_bundle_candidate', { receipt: pending('candidate'),
    confirmation: proposal('propose_bundle_candidate', { parameter: 'analysis_prompt_version', value: 'v2', reason: '试验' },
      [{ label: '实际配置差异', value: 'v1 → v2' }]) })])), false)
})

test('not-landed, queued evaluation and cancelled confirmations retain their original explanation', () => {
  for (const name of ['start_writing_intent', 'memory_status', 'save_feedback_case', 'start_data_loop']) {
    const entity = ({ start_writing_intent: 'writing_job', memory_status: 'memory', save_feedback_case: 'feedback_case', start_data_loop: 'evaluation_run' })[name]
    assert.equal(preferCapabilityCards(turn([trace(name, { receipt: pending(entity), reason: '尚未启动' })])), false)
  }
  const admission = { state: 'queued', queue_position: 1, active_count: 2, budget: 2, bootstrap_complete: true,
    error_code: null, request_workflow_id: `conversation-admission-request-${'b'.repeat(64)}` }
  assert.equal(preferCapabilityCards(turn([trace('start_data_loop', { receipt: pending('evaluation_run'),
    run: run({ phase: 'queued', evaluation_run_id: null }), admission, admission_accepted: true, workflow_accepted: false })])), false)
  assert.equal(preferCapabilityCards(turn([trace('start_writing_intent', { confirmation_outcome: 'cancelled' },
    { status: 'denied', error_code: 'confirmation_cancelled' })])), false)
  assert.equal(preferCapabilityCards(turn([trace('writing_job_status', { receipt: receipt('writing_job'), job: job(),
    admission: { ...admission, state: 'unknown' } })])), false)
})
