<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import { conversationsApi } from '@/api/conversations'
import type { WritingConfirmation } from '@/utils/writingConfirmation'
import { confirmationContent } from '@/utils/writingConfirmation'
import { formatDateTime } from '@/utils/format'
import { ApiError } from '@/api/http'

const props = defineProps<{
  confirmation: WritingConfirmation
  conversationId: string
  turnId: string
  active: boolean
  enabled: boolean
  outcome: 'started' | 'accepted' | 'cancelled' | 'expired' | null
  inputSubmitted: boolean
  originalContent: string
}>()
const emit = defineEmits<{ redescribe: [content: string] }>()
const now = ref(Date.now())
const state = ref<'pending' | 'submitting' | 'submitted' | 'retryable' | 'invalid'>('pending')
const error = ref<string | null>(null)
const selectedAction = ref<'confirm' | 'cancel' | null>(null)
let expiryTimer: ReturnType<typeof setTimeout> | null = null
let disposed = false
const expired = computed(() => now.value >= Date.parse(props.confirmation.expires_at) || props.outcome === 'expired')
const permitted = computed(() => props.enabled && props.active && !expired.value && !props.outcome
  && !props.inputSubmitted && !!props.conversationId && !!props.turnId)
const canSubmit = computed(() => permitted.value && ['pending', 'retryable'].includes(state.value))
const label = computed(() => props.outcome === 'started' ? '已确认启动' : props.outcome === 'accepted' ? '启动请求已受理' : props.outcome === 'cancelled' ? '已取消建议'
  : expired.value || !props.active || state.value === 'invalid' ? '确认已作废' : props.inputSubmitted || state.value === 'submitted' ? '确认已提交'
    : state.value === 'submitting' ? '正在提交…' : '待确认 · 模型建议')
const tone = computed(() => ({ neutral: '客观中立', explanatory: '解释说明', concise: '简洁' })[props.confirmation.suggested.tone])

watch(() => props.confirmation.expires_at, (value) => {
  if (expiryTimer !== null) clearTimeout(expiryTimer)
  now.value = Date.now()
  expiryTimer = setTimeout(() => { now.value = Date.now(); expiryTimer = null }, Math.max(0, Date.parse(value) - now.value + 1))
}, { immediate: true })
onBeforeUnmount(() => { disposed = true; if (expiryTimer !== null) clearTimeout(expiryTimer); expiryTimer = null })

async function submit(action: 'confirm' | 'cancel'): Promise<void> {
  now.value = Date.now()
  if (!canSubmit.value || selectedAction.value && selectedAction.value !== action) return
  selectedAction.value = action
  state.value = 'submitting'
  error.value = null
  try {
    const input = await conversationsApi.appendInput(props.conversationId, props.turnId, {
      request_id: props.confirmation.input_request_id,
      content: confirmationContent(props.confirmation, action),
    })
    if (disposed) return
    if (input.request_id !== props.confirmation.input_request_id || input.turn_id !== props.turnId) throw new Error('input mismatch')
    if (input.status === 'failed') throw new ApiError(409, 'input expired')
    state.value = 'submitted'
    if (action === 'cancel') emit('redescribe', props.originalContent)
  } catch (cause) {
    if (disposed) return
    if (cause instanceof ApiError && [403, 404, 409, 422].includes(cause.status)) {
      state.value = 'invalid'
      error.value = '确认已失效或权限不足，请刷新历史后重新描述。'
    } else {
      state.value = 'retryable'
      error.value = '提交结果尚未确认；可用同一确认编号重试，或刷新历史核对。'
    }
  }
}

function redescribe(): void {
  now.value = Date.now()
  if (!props.active || expired.value || state.value === 'invalid') emit('redescribe', props.originalContent)
  else void submit('cancel')
}
</script>

<template>
  <section class="na-writing-confirmation" aria-label="写作启动确认">
    <header><strong>写作启动确认</strong><span class="na-writing-confirmation__badge">{{ label }}</span></header>
    <p class="na-muted">确认后创建写作任务，研究包、提纲和终稿仍需人工确认。</p>
    <dl>
      <div><dt>主题</dt><dd>{{ confirmation.suggested.topic }}</dd></div>
      <div><dt>目标篇幅</dt><dd>{{ confirmation.suggested.target_words }} 字</dd></div>
      <div><dt>语气</dt><dd>{{ tone }}</dd></div>
    </dl>
    <details class="na-writing-confirmation__details"><summary>确认详情</summary><p class="na-muted">有效至 {{ formatDateTime(confirmation.expires_at) }}</p></details>
    <p v-if="outcome === 'started'" class="na-muted">任务已启动，进度见写作任务卡片。</p>
    <p v-else-if="outcome === 'accepted'" class="na-muted">启动请求已保存，排队和启动状态见写作任务卡片。</p>
    <p v-else-if="outcome === 'cancelled'" class="na-muted">原建议已取消，可编辑草稿后重新发送。</p>
    <p v-else-if="!active || expired || state === 'invalid'" class="na-muted">本轮确认已作废，请重新描述写作要求。</p>
    <p v-else-if="inputSubmitted || state === 'submitted'" class="na-muted">已收到确认，正在核对启动结果。</p>
    <div v-if="enabled && !outcome" class="na-row">
      <button class="na-btn na-btn--primary" type="button" :disabled="!canSubmit || selectedAction === 'cancel'" @click="submit('confirm')">
        {{ state === 'retryable' && selectedAction === 'confirm' ? '重试确认' : '确认启动' }}
      </button>
      <button class="na-btn" type="button" :disabled="active && !expired && state !== 'invalid' && (!canSubmit || selectedAction === 'confirm')" @click="redescribe">
        {{ state === 'retryable' && selectedAction === 'cancel' ? '重试取消' : '重新描述' }}
      </button>
    </div>
    <p v-if="error" class="na-message na-message--error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.na-writing-confirmation { margin-top: 12px; padding: 16px; border: 1px solid #e7c875; border-radius: 10px; background: #fffbef; }
header { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.na-writing-confirmation__badge { padding: 3px 9px; border-radius: 14px; color: #805c00; background: #fff0c2; font-size: 12px; }
dl { display: flex; gap: 12px 24px; flex-wrap: wrap; font-size: 13px; }
dt { color: var(--na-text-muted, #667085); }
dd { margin: 4px 0 0; overflow-wrap: anywhere; }
p { margin: 10px 0; font-size: 13px; }
.na-writing-confirmation__details { margin: 10px 0; color: var(--na-text-muted, #667085); font-size: 12px; }
.na-writing-confirmation__details summary { cursor: pointer; width: fit-content; }
</style>
