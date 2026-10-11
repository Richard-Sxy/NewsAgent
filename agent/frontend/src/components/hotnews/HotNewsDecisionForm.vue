<script setup lang="ts">
import { ref, watch } from 'vue'

import { hotNewsApi } from '@/api'
import type {
  DecisionType,
  FeedbackProblemType,
  FeedbackSeverity,
  JsonObject,
  OperationsFeedbackCategory,
  OperationsReasonCode,
  RecordHotNewsDecisionRequest,
  RecordHotNewsDecisionResponse,
} from '@/api'
import NaJsonBlock from '@/components/NaJsonBlock.vue'
import { useAsyncTask } from '@/composables/useAsyncTask'
import { useConfirmStore } from '@/stores/confirm'
import { useToastStore } from '@/stores/toast'
import { newIdempotencyKey } from '@/utils/format'
import {
  decisionTypeLabel,
  operationsFeedbackCategoryLabel,
  operationsReasonLabel,
  problemTypeLabel,
  severityLabel,
} from '@/utils/labels'
import {
  defaultOperationsCategory,
  defaultOperationsSeverity,
  OPERATIONS_FEEDBACK_CATEGORIES,
  OPERATIONS_REASON_CODES,
} from '@/utils/operationsFeedback'
import { firstError, jsonText, parseJson, requiredText } from '@/utils/validate'

const props = defineProps<{
  runId: string
  newsId: string
}>()

const emit = defineEmits<{
  recorded: [result: RecordHotNewsDecisionResponse]
}>()

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

const newsId = ref(props.newsId)
const decisionType = ref<DecisionType>('accepted')
const reason = ref('')
const correctionPayload = ref('')
const problemType = ref<FeedbackProblemType>('analysis_incorrect')
const feedbackCategory = ref<OperationsFeedbackCategory>(defaultOperationsCategory(decisionType.value))
const reasonCodes = ref<OperationsReasonCode[]>([])
const severity = ref<FeedbackSeverity>(defaultOperationsSeverity(feedbackCategory.value))
const severityEdited = ref(false)
const supersedes = ref('')
const result = ref<RecordHotNewsDecisionResponse | null>(null)

watch(decisionType, (value) => {
  feedbackCategory.value = defaultOperationsCategory(value)
  reasonCodes.value = []
})
watch(feedbackCategory, (value) => {
  if (!severityEdited.value) severity.value = defaultOperationsSeverity(value)
})

watch(
  () => props.newsId,
  (value) => {
    newsId.value = value
    result.value = null
  },
)

async function submit(): Promise<void> {
  const needsCorrection = decisionType.value === 'corrected'
  const problem = firstError(
    requiredText(newsId.value, 'news_id'),
    requiredText(reason.value, '原因'),
    reasonCodes.value.length === 0 ? '请至少选择一项反馈理由' : null,
    needsCorrection ? jsonText(correctionPayload.value, '修正载荷') : null,
  )
  if (problem) {
    toast.warn(problem)
    return
  }

  const confirmed = await dialog.ask({
    title: '确认记录该热点运营决策？',
    description: `${decisionTypeLabel(decisionType.value)}：${newsId.value.trim()}。将生成待标注案例，评测结论仍需独立标注与二审；运营决策不代表推送许可。`,
    confirmText: '确认记录',
    danger: decisionType.value === 'rejected',
  })
  if (!confirmed) return

  const body: RecordHotNewsDecisionRequest = {
    run_id: props.runId,
    news_id: newsId.value.trim(),
    decision_type: decisionType.value,
    reason: reason.value.trim(),
    correction_payload: needsCorrection
      ? parseJson<JsonObject>(correctionPayload.value, {})
      : {},
    idempotency_key: newIdempotencyKey('hot-news-decision'),
    supersedes_decision_id: supersedes.value.trim() || null,
    feedback_problem_type: feedbackCategory.value === 'analysis_error' ? problemType.value : 'other',
    feedback_severity: severity.value,
    operator_feedback: {
      schema_version: 'operations-feedback-v1',
      category: feedbackCategory.value,
      reason_codes: [...reasonCodes.value],
    },
  }

  const outcome = await run(() => hotNewsApi.recordDecision(body), {
    success: '热点运营决策已记录',
    failure: '记录热点运营决策失败',
  })
  if (!outcome) return
  result.value = outcome
  reason.value = ''
  correctionPayload.value = ''
  reasonCodes.value = []
  emit('recorded', outcome)
}
</script>

<template>
  <section class="na-card">
    <div class="na-card__head">
      <h2>记录热点运营决策</h2>
      <span class="na-badge na-badge--live">POST /hot-news/decisions</span>
    </div>

    <p class="na-muted">采纳、驳回、延后和修正均可沉淀为待标注案例。运营判断与评测标签分开记录，完成独立标注和二审后才可入集；这里不会授权推送。</p>

    <div class="na-row">
      <div class="na-field na-field--grow">
        <label for="hn-news-id">news_id</label>
        <input id="hn-news-id" v-model="newsId" class="na-input" autocomplete="off">
      </div>
      <div class="na-field na-field--fixed">
        <label for="hn-decision-type">决策</label>
        <select id="hn-decision-type" v-model="decisionType" class="na-select">
          <option v-for="value in DECISION_TYPES" :key="value" :value="value">
            {{ decisionTypeLabel(value) }}
          </option>
        </select>
      </div>
    </div>

    <div class="na-row">
      <div class="na-field na-field--fixed">
        <label for="hn-feedback-category">反馈分类</label>
        <select id="hn-feedback-category" v-model="feedbackCategory" class="na-select">
          <option v-for="value in OPERATIONS_FEEDBACK_CATEGORIES" :key="value" :value="value">
            {{ operationsFeedbackCategoryLabel(value) }}
          </option>
        </select>
      </div>
      <div v-if="feedbackCategory === 'analysis_error'" class="na-field na-field--fixed">
        <label for="hn-problem-type">问题类型</label>
        <select id="hn-problem-type" v-model="problemType" class="na-select">
          <option v-for="value in PROBLEM_TYPES" :key="value" :value="value">
            {{ problemTypeLabel(value) }}
          </option>
        </select>
      </div>
      <div class="na-field na-field--fixed">
        <label for="hn-severity">严重度</label>
        <select id="hn-severity" v-model="severity" class="na-select" @change="severityEdited = true">
          <option v-for="value in SEVERITIES" :key="value" :value="value">
            {{ severityLabel(value) }}
          </option>
        </select>
      </div>
      <div class="na-field na-field--grow">
        <label for="hn-supersedes">覆盖的决策 ID（可选）</label>
        <input id="hn-supersedes" v-model="supersedes" class="na-input" autocomplete="off">
      </div>
    </div>

    <div class="na-field">
      <span id="hn-feedback-reasons" class="na-muted">反馈理由（至少一项，可多选）</span>
      <div class="na-row" role="group" aria-labelledby="hn-feedback-reasons">
        <div v-for="code in OPERATIONS_REASON_CODES" :key="code" class="na-field na-field--check">
          <label>
            <input v-model="reasonCodes" type="checkbox" :value="code">
            {{ operationsReasonLabel(code) }}
          </label>
        </div>
      </div>
    </div>

    <div class="na-field">
      <label for="hn-reason">原因</label>
      <input id="hn-reason" v-model="reason" class="na-input">
    </div>

    <div v-if="decisionType === 'corrected'" class="na-field">
      <label for="hn-correction">修正载荷（JSON 对象，corrected 必填）</label>
      <textarea id="hn-correction" v-model="correctionPayload" class="na-textarea" />
    </div>

    <div class="na-row na-row--end">
      <button class="na-btn na-btn--primary" type="button" :disabled="pending" @click="submit">
        {{ pending ? '提交中…' : '提交决策' }}
      </button>
    </div>

    <NaJsonBlock v-if="result" :value="result" label="提交结果" />
  </section>
</template>
