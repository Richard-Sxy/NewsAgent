import { computed, onBeforeUnmount, ref, watch } from 'vue'

import { conversationsApi } from '@/api/conversations'
import { ApiError } from '@/api/http'
import type { CapabilityConfirmation } from '@/utils/capabilityConfirmation'

export interface CapabilityConfirmationProps {
  confirmation: CapabilityConfirmation
  conversationId: string
  turnId: string
  active: boolean
  enabled: boolean
  outcome: 'completed' | 'accepted' | 'cancelled' | 'expired' | null
  inputSubmitted: boolean
  originalContent: string
}

/** Confirmation transport is separate from the capability's suggested business fields. */
export function useCapabilityConfirmation(props: CapabilityConfirmationProps, onRedescribe: (content: string) => void) {
  const now = ref(Date.now())
  const state = ref<'pending' | 'submitting' | 'submitted' | 'retryable' | 'invalid'>('pending')
  const error = ref<string | null>(null)
  const selectedAction = ref<'confirm' | 'cancel' | null>(null)
  let expiryTimer: ReturnType<typeof setTimeout> | null = null
  let disposed = false
  const expired = computed(() => now.value >= Date.parse(props.confirmation.expires_at) || props.outcome === 'expired')
  const canSubmit = computed(() => props.enabled && props.active && !expired.value && !props.outcome && !props.inputSubmitted
    && !!props.conversationId && !!props.turnId && ['pending', 'retryable'].includes(state.value))
  const label = computed(() => props.outcome === 'completed' ? (props.confirmation.hook === 'start_data_loop' ? '已确认启动' : '已确认保存')
    : props.outcome === 'accepted' ? '启动请求已受理' : props.outcome === 'cancelled' ? '已取消建议'
    : expired.value || !props.active || state.value === 'invalid' ? '确认已作废' : props.inputSubmitted || state.value === 'submitted' ? '确认已提交'
      : state.value === 'submitting' ? '正在提交…' : '待确认 · 模型建议')
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
        content: `capability-confirm:${JSON.stringify({ token: props.confirmation.token, action })}`,
      })
      if (disposed) return
      if (input.request_id !== props.confirmation.input_request_id || input.turn_id !== props.turnId) throw new Error('input mismatch')
      if (input.status === 'failed') throw new ApiError(409, 'input expired')
      state.value = 'submitted'
      if (action === 'cancel') onRedescribe(props.originalContent)
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
    if (!props.active || expired.value || state.value === 'invalid') onRedescribe(props.originalContent)
    else void submit('cancel')
  }
  return { state, error, selectedAction, expired, canSubmit, label, submit, redescribe }
}
