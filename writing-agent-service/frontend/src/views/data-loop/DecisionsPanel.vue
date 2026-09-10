<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { dataLoopApi } from '@/api'
import type {
  DecisionType,
  FeedbackCase,
  FeedbackProblemType,
  FeedbackSeverity,
  JsonObject,
  OperatorDecisionResponse,
  RecordOperatorDecisionRequest,
} from '@/api'
import NaJsonBlock from '@/components/NaJsonBlock.vue'
import NaStateBlock from '@/components/NaStateBlock.vue'
import { useAsyncTask } from '@/composables/useAsyncTask'
import { useConfirmStore } from '@/stores/confirm'
import { useToastStore } from '@/stores/toast'
import { describeError } from '@/utils/errors'
import { newIdempotencyKey } from '@/utils/format'
import { decisionTypeLabel, problemTypeLabel, severityLabel } from '@/utils/labels'
import { firstError, jsonText, parseJson, requiredText } from '@/utils/validate'

const toast = useToastStore()
const dialog = useConfirmStore()
const { pending, run } = useAsyncTask()

const DECISION_TYPES: DecisionType[] = ['accepted', 'rejected', 'deferred', 'corrected']
const PROBLEM_TYPES: FeedbackProblemType[] = [
  'analysis_incorrect',
  'unsupported_claim',
  'metric_mismatch',
  'evidence_mismatch',
  'missing_evidence',
  'low_confidence',
  'retrieval_miss',
  'retrieval_false_positive',
  'schema_violation',
  'policy_violation',
  'outcome_underperformance',
  'other',
]
const SEVERITIES: FeedbackSeverity[] = ['low', 'medium', 'high', 'critical']

const runId = ref('')
const newsId = ref('')
const decisionType = ref<DecisionType>('rejected')
const reason = ref('')
const correctionPayload = ref('')
const problemType = ref<FeedbackProblemType>('analysis_incorrect')
const severity = ref<FeedbackSeverity>('medium')
const supersedes = ref('')

const result = ref<OperatorDecisionResponse | null>(null)

const recentCases = ref<FeedbackCase[]>([])
const casesLoading = ref(false)
const casesError = ref<string | null>(null)

async function loadRecentCases(): Promise<void> {
  casesLoading.value = true
  casesError.value = null
  try {
    const page = await dataLoopApi.listFeedbackCases({ limit: 20 })
    recentCases.value = page.cases
    if (page.cases.length === 0) toast.info('后端暂无可带入的反馈案例')
  } catch (cause) {
    casesError.value = describeError(cause)
  } finally {
    casesLoading.value = false
  }
}

function applyCase(event: Event): void {
  const target = event.target as HTMLSelectElement
  const picked = recentCases.value.find((item) => item.id === target.value)
  if (!picked) return
  runId.value = picked.run_id ?? ''
  newsId.value = picked.news_id
  problemType.value = picked.problem_type
  severity.value = picked.severity
}

async function submit(): Promise<void> {
  const needsCorrection = decisionType.value === 'corrected'
  const problem = firstError(
    requiredText(runId.value, 'run_id'),
    requiredText(newsId.value, 'news_id'),
    requiredText(reason.value, '原因'),
    needsCorrection ? jsonText(correctionPayload.value, '修正载荷') : null,
  )
  if (problem) {
    toast.warn(problem)
    return
  }

  const confirmed = await dialog.ask({
    title: '确认记录该运营决策？',
    description: `${decisionTypeLabel(decisionType.value)}：${newsId.value.trim()}。决策会进入数据回流链路。`,
    confirmText: '确认记录',
    danger: decisionType.value === 'rejected',
  })
  if (!confirmed) return

  const body: RecordOperatorDecisionRequest = {
    run_id: runId.value.trim(),
    news_id: newsId.value.trim(),
    decision_type: decisionType.value,
    reason: reason.value.trim(),
    correction_payload: needsCorrection
      ? parseJson<JsonObject>(correctionPayload.value, {})
      : {},
    idempotency_key: newIdempotencyKey('operator-decision'),
    supersedes_decision_id: supersedes.value.trim() || null,
    feedback_problem_type: problemType.value,
    feedback_severity: severity.value,
  }

  const outcome = await run(() => dataLoopApi.recordOperatorDecision(body), {
    success: '运营决策已记录',
    failure: '记录运营决策失败',
  })
  if (!outcome) return
  result.value = outcome
  reason.value = ''
  correctionPayload.value = ''
}

onMounted(loadRecentCases)
</script>

<template>
  <div class="na-grid na-grid--split">
    <section class="na-card">
      <div class="na-card__head">
        <h2>记录运营决策</h2>
        <span class="na-badge na-badge--live">POST /data-loop/operator-decisions</span>
      </div>

      <div class="na-row">
        <div class="na-field na-field--grow">
          <label for="run-id">run_id</label>
          <input id="run-id" v-model="runId" class="na-input" autocomplete="off">
        </div>
        <div class="na-field na-field--grow">
          <label for="news-id">news_id</label>
          <input id="news-id" v-model="newsId" class="na-input" autocomplete="off">
        </div>
      </div>

      <div class="na-row">
        <div class="na-field na-field--fixed">
          <label for="decision-type">决策</label>
          <select id="decision-type" v-model="decisionType" class="na-select">
            <option v-for="value in DECISION_TYPES" :key="value" :value="value">
              {{ decisionTypeLabel(value) }}
            </option>
          </select>
        </div>
        <div class="na-field na-field--fixed">
          <label for="problem-type">问题类型</label>
          <select id="problem-type" v-model="problemType" class="na-select">
            <option v-for="value in PROBLEM_TYPES" :key="value" :value="value">
              {{ problemTypeLabel(value) }}
            </option>
          </select>
        </div>
        <div class="na-field na-field--fixed">
          <label for="severity">严重度</label>
          <select id="severity" v-model="severity" class="na-select">
            <option v-for="value in SEVERITIES" :key="value" :value="value">
              {{ severityLabel(value) }}
            </option>
          </select>
        </div>
      </div>

      <div class="na-field">
        <label for="reason">原因</label>
        <input id="reason" v-model="reason" class="na-input">
      </div>

      <div class="na-field">
        <label for="supersedes">覆盖的决策 ID（可选）</label>
        <input id="supersedes" v-model="supersedes" class="na-input" autocomplete="off">
      </div>

      <div v-if="decisionType === 'corrected'" class="na-field">
        <label for="correction">修正载荷（JSON 对象，corrected 必填）</label>
        <textarea
          id="correction"
          v-model="correctionPayload"
          class="na-textarea"
          placeholder="例如：{&quot;headline&quot;: &quot;修正后的标题&quot;}"
        />
      </div>

      <div class="na-row na-row--end">
        <button class="na-btn na-btn--primary" type="button" :disabled="pending" @click="submit">
          {{ pending ? '提交中…' : '提交决策' }}
        </button>
      </div>

      <NaJsonBlock v-if="result" :value="result" label="提交结果" />
    </section>

    <section class="na-card">
      <div class="na-card__head">
        <h2>从反馈案例带入</h2>
        <button class="na-btn" type="button" :disabled="casesLoading" @click="loadRecentCases">
          {{ casesLoading ? '加载中…' : '重新加载' }}
        </button>
      </div>

      <p class="na-muted">选择一条最近的反馈案例，自动填充 run_id、news_id、问题类型与严重度。</p>

      <NaStateBlock
        :loading="casesLoading && recentCases.length === 0"
        :error="casesError"
        :empty="recentCases.length === 0"
        empty-text="暂无可带入的案例"
        loading-text="加载案例…"
      >
        <div class="na-field">
          <label for="case-prefill">反馈案例</label>
          <select id="case-prefill" class="na-select" @change="applyCase">
            <option value="">请选择…</option>
            <option v-for="item in recentCases" :key="item.id" :value="item.id">
              {{ item.news_id }} · {{ problemTypeLabel(item.problem_type) }}
            </option>
          </select>
        </div>

        <NaJsonBlock :value="recentCases.slice(0, 3)" label="最近 3 条案例（供核对）" />
      </NaStateBlock>
    </section>
  </div>
</template>
