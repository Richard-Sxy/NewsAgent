<script setup lang="ts">
import { ref, watch } from 'vue'

import { hotNewsApi } from '@/api'
import type {
  DecisionType,
  FeedbackProblemType,
  FeedbackSeverity,
  JsonObject,
  RecordHotNewsDecisionRequest,
  RecordHotNewsDecisionResponse,
} from '@/api'
import NaJsonBlock from '@/components/NaJsonBlock.vue'
import { useAsyncTask } from '@/composables/useAsyncTask'
import { useConfirmStore } from '@/stores/confirm'
import { useToastStore } from '@/stores/toast'
import { newIdempotencyKey } from '@/utils/format'
import { decisionTypeLabel, problemTypeLabel, severityLabel } from '@/utils/labels'
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
const severity = ref<FeedbackSeverity>('medium')
const supersedes = ref('')
const result = ref<RecordHotNewsDecisionResponse | null>(null)

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
    needsCorrection ? jsonText(correctionPayload.value, '修正载荷') : null,
  )
  if (problem) {
    toast.warn(problem)
    return
  }

  const confirmed = await dialog.ask({
    title: '确认记录该热点运营决策？',
    description: `${decisionTypeLabel(decisionType.value)}：${newsId.value.trim()}。拒绝与修正会进入 Data Loop 反馈链路。`,
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
    feedback_problem_type: problemType.value,
    feedback_severity: severity.value,
  }

  const outcome = await run(() => hotNewsApi.recordDecision(body), {
    success: '热点运营决策已记录',
    failure: '记录热点运营决策失败',
  })
  if (!outcome) return
  result.value = outcome
  reason.value = ''
  correctionPayload.value = ''
  emit('recorded', outcome)
}
</script>

<template>
  <section class="na-card">
    <div class="na-card__head">
      <h2>记录热点运营决策</h2>
      <span class="na-badge na-badge--live">POST /hot-news/decisions</span>
    </div>

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
        <label for="hn-problem-type">问题类型</label>
        <select id="hn-problem-type" v-model="problemType" class="na-select">
          <option v-for="value in PROBLEM_TYPES" :key="value" :value="value">
            {{ problemTypeLabel(value) }}
          </option>
        </select>
      </div>
      <div class="na-field na-field--fixed">
        <label for="hn-severity">严重度</label>
        <select id="hn-severity" v-model="severity" class="na-select">
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
