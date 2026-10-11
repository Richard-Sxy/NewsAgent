import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
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

const source = (path) => readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8')
const labels = load(source('utils/labels.ts'))
const feedback = load(source('utils/operationsFeedback.ts'), { '@/utils/labels': labels })
const validate = load(source('utils/validate.ts'))

function panel(t, path, options = {}) {
  const mutations = []
  const queries = []
  const warnings = []
  const confirmations = []
  const mounted = []
  const record = async (body) => {
    mutations.push(body)
    return { decision_id: 'decision-1', decision_type: body.decision_type, created: true }
  }
  const api = {
    recordDecision: record,
    recordOperatorDecision: record,
    listFeedbackCases: async (query) => {
      queries.push(query)
      return options.list ? options.list(query) : { cases: [] }
    },
    listLabels: async () => ({ labels: [] }),
  }
  const compiled = compileScript(parse(source(path)).descriptor, { id: 'operations-feedback-test' }).content
  const component = load(compiled, {
    vue: { ...vue, onMounted: (callback) => mounted.push(callback) },
    '@/api': { hotNewsApi: api, dataLoopApi: api },
    '@/components/NaJsonBlock.vue': { default: {} },
    '@/components/NaStateBlock.vue': { default: {} },
    '@/composables/useAsyncTask': { useAsyncTask: () => ({ pending: vue.ref(false), run: (action) => action() }) },
    '@/stores/confirm': { useConfirmStore: () => ({ ask: async (question) => {
      confirmations.push(question)
      return options.confirm ?? true
    } }) },
    '@/stores/toast': { useToastStore: () => ({ warn: (message) => warnings.push(message), info() {} }) },
    '@/utils/errors': { describeError: (error) => error.message },
    '@/utils/format': { newIdempotencyKey: () => 'idempotent', formatDateTime: () => '' },
    '@/utils/labels': labels,
    '@/utils/operationsFeedback': feedback,
    '@/utils/validate': validate,
  }).default
  const scope = vue.effectScope()
  const state = scope.run(() => component.setup({ runId: 'run-1', newsId: 'news-1' }, { expose() {}, emit() {} }))
  t.after(() => scope.stop())
  return { state, mutations, queries, warnings, confirmations, mount: () => Promise.all(mounted.map((callback) => callback())) }
}

for (const path of ['components/hotnews/HotNewsDecisionForm.vue', 'views/data-loop/DecisionsPanel.vue']) {
  test(`${path}: all decisions submit business feedback without claiming a reviewed verdict`, async (t) => {
    const { state, mutations, confirmations } = panel(t, path)
    if (state.runId) state.runId.value = 'run-1'
    state.newsId.value = 'news-1'
    for (const [decision, category, code, severity] of [
      ['accepted', 'positive_example', 'analysis_supported', 'low'],
      ['deferred', 'strategy_judgment', 'timing_inappropriate', 'low'],
      ['rejected', 'analysis_error', 'insufficient_evidence', 'medium'],
      ['corrected', 'analysis_error', 'fact_or_attribution_error', 'medium'],
    ]) {
      state.decisionType.value = decision
      await vue.nextTick()
      state.reason.value = '运营已核对该案例'
      state.reasonCodes.value = [code]
      state.correctionPayload.value = '{"summary":"更正事实"}'
      await state.submit()
      const body = mutations.at(-1)
      assert.equal(body.decision_type, decision)
      assert.deepEqual(body.operator_feedback, { schema_version: 'operations-feedback-v1', category, reason_codes: [code] })
      assert.equal(body.feedback_severity, severity)
      assert.equal(body.feedback_problem_type, category === 'analysis_error' ? 'analysis_incorrect' : 'other')
      assert.deepEqual(body.correction_payload, decision === 'corrected' ? { summary: '更正事实' } : {})
      assert.equal(Object.hasOwn(body, 'verdict'), false)
      assert.deepEqual(state.reasonCodes.value, [])
      assert.match(confirmations.at(-1).description, /独立标注与二审.*不代表推送许可/)
    }
    assert.equal(mutations.length, 4)
  })

  test(`${path}: an editorial rejection preserves explicit risk severity and is not an analysis error`, async (t) => {
    const { state, mutations } = panel(t, path)
    if (state.runId) state.runId.value = 'run-1'
    state.newsId.value = 'news-1'
    state.decisionType.value = 'rejected'
    await vue.nextTick()
    state.severity.value = 'critical'
    state.severityEdited.value = true
    state.feedbackCategory.value = 'editorial_judgment'
    await vue.nextTick()
    state.reason.value = '公共价值不足，风险需升级'
    state.reasonCodes.value = ['low_public_value', 'risk_requires_escalation']
    await state.submit()
    assert.equal(mutations[0].feedback_problem_type, 'other')
    assert.equal(mutations[0].feedback_severity, 'critical')
    assert.equal(mutations[0].operator_feedback.category, 'editorial_judgment')
  })

  test(`${path}: required reason codes, correction JSON and cancellation prevent mutations`, async (t) => {
    const { state, mutations, warnings } = panel(t, path, { confirm: false })
    if (state.runId) state.runId.value = 'run-1'
    state.newsId.value = 'news-1'
    state.reason.value = '已核对'
    await state.submit()
    assert.match(warnings.at(-1), /至少选择一项/)
    state.decisionType.value = 'corrected'
    await vue.nextTick()
    state.reasonCodes.value = ['fact_or_attribution_error']
    state.correctionPayload.value = '[]'
    await state.submit()
    assert.match(warnings.at(-1), /JSON 对象/)
    state.correctionPayload.value = '{}'
    await state.submit()
    assert.equal(mutations.length, 0)
  })
}

test('case prefill carries operator classification without synthesizing a label', async (t) => {
  const { state, mount } = panel(t, 'views/data-loop/DecisionsPanel.vue', {
    list: () => ({ cases: [{ id: 'case-1', run_id: 'run-1', news_id: 'news-2', problem_type: 'other', severity: 'high',
      source_reference: { operator_feedback: { category: 'strategy_judgment', reason_codes: ['frequency_budget_exhausted'] } } },
    { id: 'legacy-case', run_id: 'run-2', news_id: 'news-3', problem_type: 'missing_evidence', severity: 'medium', source_reference: {} }] }),
  })
  await mount()
  state.applyCase({ target: { value: 'case-1' } })
  await vue.nextTick()
  assert.equal(state.newsId.value, 'news-2')
  assert.equal(state.feedbackCategory.value, 'strategy_judgment')
  assert.equal(state.severity.value, 'high')
  assert.deepEqual(state.reasonCodes.value, ['frequency_budget_exhausted'])
  state.applyCase({ target: { value: 'legacy-case' } })
  await vue.nextTick()
  assert.equal(state.feedbackCategory.value, 'analysis_error')
  assert.deepEqual(state.reasonCodes.value, [])
  assert.equal(state.severity.value, 'medium')
})

test('feedback case pagination loads beyond 50 and uses the selected status on every page', async (t) => {
  const cases = Array.from({ length: 52 }, (_, index) => ({ id: `case-${index}`, source_reference: {} }))
  const { state, queries, mount } = panel(t, 'views/data-loop/FeedbackCasesPanel.vue', {
    list: ({ offset, limit }) => ({ cases: cases.slice(offset, offset + limit) }),
  })
  await mount()
  assert.equal(state.cases.value.length, 50)
  assert.equal(state.hasNextPage.value, true)
  state.statusFilter.value = 'needs_label'
  await state.reload(50)
  assert.deepEqual(queries.at(-1), { statuses: ['needs_label'], offset: 50, limit: 51 })
  assert.equal(state.pageOffset.value, 50)
  assert.deepEqual(state.cases.value.map((item) => item.id), ['case-50', 'case-51'])
  assert.equal(state.hasNextPage.value, false)
  await state.reload(0)
  assert.equal(state.pageOffset.value, 0)
})

test('feedback API emits statuses and offset using the backend query contract', async () => {
  const calls = []
  const { dataLoopApi } = load(source('api/dataLoop.ts'), { '@/api/http': {
    request: async (...arguments_) => { calls.push(arguments_); return { cases: [] } },
  } })
  await dataLoopApi.listFeedbackCases({ statuses: ['labeled'], offset: 50, limit: 51 })
  assert.deepEqual(calls[0], ['/api/v1/data-loop/feedback-cases', {
    query: { statuses: ['labeled'], offset: 50, limit: 51 },
  }])
})

test('operator classification display is optional for legacy or malformed source references', () => {
  assert.equal(feedback.readOperationsFeedback({ decision_type: 'accepted' }), null)
  assert.equal(feedback.readOperationsFeedback({ operator_feedback: { category: 'made-up', reason_codes: [] } }), null)
  assert.equal(feedback.readOperationsFeedback({ operator_feedback: { category: 'analysis_error', reason_codes: 'wrong' } }), null)
  assert.equal(feedback.readOperationsFeedback({ operator_feedback: { category: 'analysis_error', reason_codes: ['unrecognized'] } }), null)
  assert.equal(labels.feedbackSourceLabel('operator_accepted'), '运营采纳')
  assert.equal(labels.feedbackSourceLabel('operator_deferred'), '运营延后')
})
