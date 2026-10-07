import { request } from '@/api/http'
import { streamConversationMessage } from '@/api/conversationStream'
import type { ConversationStreamOptions } from '@/api/conversationStream'

export interface Conversation {
  id: string
  title: string
  created_at: string
  updated_at: string
}

export interface ConversationToolTrace {
  name: string
  status: 'completed' | 'failed' | 'denied'
  attempts: number
  arguments: Record<string, unknown>
  result: Record<string, unknown>
  error_code: string | null
}

export interface ConversationTurn {
  id: string
  request_id: string
  user_content: string
  assistant_content: string | null
  status: 'processing' | 'completed' | 'failed'
  tools: ConversationToolTrace[]
  model_request_ids: string[]
  error_code: string | null
  created_at: string
  completed_at: string | null
}

export interface ConversationDetail {
  conversation: Conversation
  turns: ConversationTurn[]
}

export interface SendConversationMessage {
  request_id: string
  content: string
}

export interface ConversationRuntime {
  model_provider: 'local' | 'openai_compatible' | 'unknown'
  model_route: string
  prompt_version: string
  query_enabled: boolean
  query_scope: { scenario_id: string; window_start: string; window_end: string } | null
  data_analysis?: {
    name: string
    execution_backend: 'process' | 'docker' | 'service'
    description?: string
    arguments?: Record<string, string>
    resource_limits?: Record<string, number | boolean>
  } | null
}

const BASE = '/api/v1/conversations'

/** Cookie 与身份头沿用现有网关链路；浏览器不保存或设置身份 Token。 */
export const conversationsApi = {
  runtime() {
    return request<ConversationRuntime>(`${BASE}/runtime`)
  },
  list() {
    return request<{ items: Conversation[] }>(BASE)
  },

  create(title?: string, options: { signal?: AbortSignal } = {}) {
    return request<Conversation>(BASE, {
      method: 'POST', body: title ? { title } : {},
      ...(options.signal ? { signal: options.signal } : {}),
    })
  },

  detail(conversationId: string) {
    return request<ConversationDetail>(`${BASE}/${encodeURIComponent(conversationId)}`)
  },

  remove(conversationId: string) {
    return request<void>(`${BASE}/${encodeURIComponent(conversationId)}`, { method: 'DELETE' })
  },

  restore(conversationId: string) {
    return request<Conversation>(`${BASE}/${encodeURIComponent(conversationId)}/restore`, { method: 'POST' })
  },

  send(conversationId: string, body: SendConversationMessage, options: { signal?: AbortSignal } = {}) {
    return request<ConversationTurn>(`${BASE}/${encodeURIComponent(conversationId)}/messages`, {
      method: 'POST',
      body,
      timeoutMs: 120_000,
      ...(options.signal ? { signal: options.signal } : {}),
    })
  },

  sendStream(
    conversationId: string,
    body: SendConversationMessage,
    options: ConversationStreamOptions,
  ) {
    return streamConversationMessage(
      `${BASE}/${encodeURIComponent(conversationId)}/messages/stream`,
      body,
      options,
    )
  },
}
