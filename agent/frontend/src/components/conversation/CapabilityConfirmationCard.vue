<script setup lang="ts">
import { computed } from 'vue'
import { useCapabilityConfirmation } from '@/composables/useCapabilityConfirmation'
import type { CapabilityConfirmation } from '@/utils/capabilityConfirmation'
import { formatDateTime } from '@/utils/format'
import { problemTypeLabel } from '@/utils/labels'

const props = defineProps<{
  confirmation: CapabilityConfirmation
  conversationId: string
  turnId: string
  active: boolean
  enabled: boolean
  outcome: 'completed' | 'accepted' | 'cancelled' | 'expired' | null
  inputSubmitted: boolean
  originalContent: string
}>()
const emit = defineEmits<{ redescribe: [content: string] }>()
const { state, error, selectedAction, expired, canSubmit, label, submit, redescribe } = useCapabilityConfirmation(props,
  (content) => emit('redescribe', content))
const copy = computed(() => props.confirmation.hook === 'start_data_loop' ? {
  title: '评测启动确认', notice: '确认后开始评测，通过后仍需人工审批。',
  confirmed: '评测已受理，状态见评测运行卡片。', submit: '确认启动',
} : props.confirmation.hook === 'propose_bundle_candidate' ? {
  title: '配置候选提交确认', notice: '候选提交后需先通过评测，再进入人工审批。',
  confirmed: '候选已保存，详情见配置候选回执。', submit: '确认提交',
} : {
  title: '反馈案例保存确认', notice: '同时记录本次分析驳回反馈；期望标签仅草稿。',
  confirmed: '案例已保存，结果见反馈案例卡片。', submit: '确认保存',
})
const technicalLabels = new Set(['运行 ID', '热点运行 ID', '生产 Bundle', '生产规则包版本', '基准 Bundle ID',
  '基准 Bundle 版本', '候选版本', '黄金集 ID', '高风险集 ID', '门禁策略版本'])
const visibleFields = computed(() => props.confirmation.fields.filter((field) => !technicalLabels.has(field.label)
  && !(field.label === '新闻 ID' && props.confirmation.fields.some((item) => item.label === '新闻标题'))))
const technicalFields = computed(() => props.confirmation.fields.filter((field) => !visibleFields.value.includes(field)))

function displayLabel(label: string): string {
  return ({ cohorts: '冻结数据集来源（三层）', window_start: '错误案例来源窗口起点',
    window_end: '来源截止时间', '候选 ID': '评测对象' } as Record<string, string>)[label] ?? label
}

function displayValue(field: { label: string; value: string }): string {
  const label = displayLabel(field.label)
  if (label === '同时执行的反馈动作' && field.value === 'reject_analysis') return '驳回本次分析'
  if (label === '问题类型') return problemTypeLabel(field.value)
  if (['错误案例来源窗口起点', '来源截止时间'].includes(label) && Number.isFinite(Date.parse(field.value))) return formatDateTime(field.value)
  if (label === '实际配置差异') {
    try {
      const changes: unknown = JSON.parse(field.value)
      if (Array.isArray(changes) && changes.length && changes.every((change) => change && typeof change === 'object'
        && typeof change.asset === 'string' && typeof change.before_version === 'string' && typeof change.after_version === 'string')) {
        return changes.map((change) => `${({ model: '模型', model_version: '模型', analysis_prompt: '分析 Prompt' } as Record<string, string>)[change.asset] ?? change.asset}：${change.before_version} → ${change.after_version}`).join('\n')
      }
    } catch { /* Unknown formats remain visible as the original server text. */ }
  }
  if (label === '冻结数据集来源（三层）') {
    try {
      const cohorts: unknown = JSON.parse(field.value)
      if (cohorts && typeof cohorts === 'object' && !Array.isArray(cohorts)) {
        const data = cohorts as Record<string, unknown>
        const layers = ['golden', 'fresh_bad_case', 'high_risk_regression']
        if (layers.every((layer) => data[layer] && typeof data[layer] === 'object' && !Array.isArray(data[layer])
          && Number.isSafeInteger((data[layer] as Record<string, unknown>).case_count))) {
          return layers.map((layer) => `${({ golden: '已冻结黄金集', fresh_bad_case: '错误案例来源集', high_risk_regression: '已冻结高风险集' } as Record<string, string>)[layer]}：${(data[layer] as Record<string, unknown>).case_count} 例`).join('\n')
        }
      }
    } catch { /* Preserve unknown cohort formats for review. */ }
  }
  return field.value
}
</script>

<template>
  <section class="na-capability-confirmation" :aria-label="copy.title">
    <header><strong>{{ copy.title }}</strong><span class="na-capability-confirmation__badge">{{ label }}</span></header>
    <p class="na-muted">{{ copy.notice }}</p>
    <dl class="na-capability-confirmation__fields">
      <div v-for="(field, index) in visibleFields" :key="index"><dt>{{ displayLabel(field.label) }}</dt><dd>{{ displayValue(field) }}</dd></div>
    </dl>
    <details class="na-capability-confirmation__details">
      <summary>确认详情</summary>
      <dl>
        <div v-for="(field, index) in technicalFields" :key="index"><dt>{{ field.label }}</dt><dd>{{ field.value }}</dd></div>
        <div v-for="(field, index) in visibleFields.filter((item) => displayValue(item) !== item.value)" :key="`source-${index}`"><dt>{{ field.label }}原始记录</dt><dd>{{ field.value }}</dd></div>
      </dl>
      <p class="na-muted">有效至 {{ formatDateTime(confirmation.expires_at) }}</p>
    </details>
    <p v-if="outcome === 'completed'" class="na-muted">{{ copy.confirmed }}</p>
    <p v-else-if="outcome === 'accepted'" class="na-muted">启动请求已保存，排队和启动状态见评测运行卡片。</p>
    <p v-else-if="outcome === 'cancelled'" class="na-muted">原建议已取消，可编辑草稿后重新发送。</p>
    <p v-else-if="!active || expired || state === 'invalid'" class="na-muted">本轮确认已作废，请重新描述要求。</p>
    <p v-else-if="inputSubmitted || state === 'submitted'" class="na-muted">已收到确认，正在核对执行结果。</p>
    <div v-if="enabled && !outcome" class="na-row">
      <button class="na-btn na-btn--primary" type="button" :disabled="!canSubmit || selectedAction === 'cancel'" @click="submit('confirm')">
        {{ state === 'retryable' && selectedAction === 'confirm' ? '重试确认' : copy.submit }}
      </button>
      <button class="na-btn" type="button" :disabled="active && !expired && state !== 'invalid' && (!canSubmit || selectedAction === 'confirm')" @click="redescribe">
        {{ state === 'retryable' && selectedAction === 'cancel' ? '重试取消' : '重新描述' }}
      </button>
    </div>
    <p v-if="error" class="na-message na-message--error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.na-capability-confirmation { margin-top: 12px; padding: 16px; border: 1px solid #e7c875; border-radius: 10px; background: #fffbef; }
header { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.na-capability-confirmation__badge { padding: 3px 9px; border-radius: 14px; color: #805c00; background: #fff0c2; font-size: 12px; }
dl { display: flex; gap: 12px 24px; flex-wrap: wrap; font-size: 13px; }
.na-capability-confirmation__fields > div { flex: 1 1 220px; }
.na-capability-confirmation__details { margin: 10px 0; color: var(--na-text-muted, #667085); font-size: 12px; }
.na-capability-confirmation__details summary { cursor: pointer; width: fit-content; }
dt { color: var(--na-text-muted, #667085); }
dd { margin: 4px 0 0; overflow-wrap: anywhere; white-space: pre-wrap; }
p { margin: 10px 0; font-size: 13px; }
</style>
