import type { ConversationInput, ConversationToolTrace } from '@/api/conversations'
import { parseCapabilityReceipt, writingJobId } from '@/utils/capabilityReceipts'
import { parseAdmission } from '@/utils/admission'

export interface WritingConfirmation {
  token: string
  hook: 'start_writing_intent'
  expires_at: string
  input_request_id: string
  suggested: { topic: string; target_words: number; tone: 'neutral' | 'explanatory' | 'concise' }
  state: 'pending'
  provenance: 'model_suggestion'
}

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}

export function writingConfirmation(trace: ConversationToolTrace): WritingConfirmation | null {
  if (trace.name !== 'start_writing_intent' || trace.status !== 'completed') return null
  const receipt = parseCapabilityReceipt(trace.result.receipt)
  const value = object(trace.result.confirmation)
  const suggested = object(value?.suggested)
  if (!receipt || receipt.status !== 'not_landed' || receipt.entity_type !== 'writing_job' || !value || !suggested
    || value.hook !== 'start_writing_intent' || value.state !== 'pending' || value.provenance !== 'model_suggestion'
    || typeof value.token !== 'string' || !/^[A-Za-z0-9_-]{16,256}$/.test(value.token)
    || !writingJobId(value.input_request_id) || typeof value.expires_at !== 'string'
    || !/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value.expires_at) || !Number.isFinite(Date.parse(value.expires_at))
    || typeof suggested.topic !== 'string' || !suggested.topic.trim() || suggested.topic.length > 500
    || typeof suggested.target_words !== 'number' || !Number.isSafeInteger(suggested.target_words) || suggested.target_words < 1
    || suggested.target_words > 10000 || !['neutral', 'explanatory', 'concise'].includes(String(suggested.tone))) return null
  return value as unknown as WritingConfirmation
}

export function confirmationOutcome(tools: ConversationToolTrace[]): 'started' | 'accepted' | 'cancelled' | 'expired' | null {
  for (const trace of tools) {
    if (trace.name !== 'start_writing_intent') continue
    const receipt = parseCapabilityReceipt(trace.result.receipt)
    if (trace.status === 'completed' && receipt?.entity_type === 'writing_job' && receipt.entity_id) {
      if (trace.result.workflow_accepted === true) return 'started'
      if (trace.result.admission_accepted === true && parseAdmission(trace.result.admission)) return 'accepted'
    }
    if (trace.result.confirmation_outcome === 'cancelled') return 'cancelled'
    if (trace.result.confirmation_outcome === 'expired') return 'expired'
  }
  return null
}

export function confirmationInput(inputs: ConversationInput[] | undefined, confirmation: WritingConfirmation): boolean {
  return Array.isArray(inputs) && inputs.some((input) => input?.request_id === confirmation.input_request_id && ['pending', 'applied'].includes(input.status))
}

export function confirmationContent(confirmation: WritingConfirmation, action: 'confirm' | 'cancel'): string {
  return `capability-confirm:${JSON.stringify({ token: confirmation.token, action })}`
}
