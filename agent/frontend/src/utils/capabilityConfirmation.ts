import type { ConversationInput, ConversationToolTrace } from '@/api/conversations'
import { dataLoopWorkflowId, parseCapabilityReceipt, writingJobId } from '@/utils/capabilityReceipts'
import { parseAdmission } from '@/utils/admission'
import { PROBLEM_TYPE_LABELS } from '@/utils/labels'

export interface CapabilityConfirmation {
  token: string
  hook: 'save_feedback_case' | 'start_data_loop' | 'propose_bundle_candidate'
  expires_at: string
  input_request_id: string
  suggested: Record<string, unknown>
  fields: Array<{ label: string; value: string }>
  state: 'pending'
  provenance: 'model_suggestion'
}

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}

export function capabilityConfirmation(trace: ConversationToolTrace): CapabilityConfirmation | null {
  const entity = ({ save_feedback_case: 'feedback_case', start_data_loop: 'evaluation_run', propose_bundle_candidate: 'candidate' } as Record<string, string>)[trace.name]
  if (!entity || trace.status !== 'completed') return null
  const receipt = parseCapabilityReceipt(trace.result.receipt)
  const value = object(trace.result.confirmation)
  const suggested = object(value?.suggested)
  if (!receipt || receipt.status !== 'not_landed' || receipt.entity_type !== entity || !value || !suggested
    || value.hook !== trace.name || value.state !== 'pending' || value.provenance !== 'model_suggestion'
    || typeof value.token !== 'string' || !/^[A-Za-z0-9_-]{16,256}$/.test(value.token)
    || !writingJobId(value.input_request_id) || typeof value.expires_at !== 'string'
    || !/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value.expires_at) || !Number.isFinite(Date.parse(value.expires_at))
    || !Array.isArray(value.fields) || !value.fields.length || value.fields.length > 12) return null
  if (trace.name === 'save_feedback_case' && (typeof suggested.run_id !== 'string' || !suggested.run_id
    || typeof suggested.news_id !== 'string' || !suggested.news_id || typeof suggested.issue_type !== 'string'
    || !Object.hasOwn(PROBLEM_TYPE_LABELS, suggested.issue_type) || typeof suggested.expected_label_draft !== 'string'
    || !suggested.expected_label_draft || suggested.expected_label_draft.length > 96)) return null
  if (trace.name === 'start_data_loop' && (!writingJobId(suggested.candidate_id)
    || Object.keys(suggested).some((key) => key !== 'candidate_id'))) return null
  if (trace.name === 'propose_bundle_candidate' && (
    !['model_version', 'analysis_prompt_version'].includes(String(suggested.parameter))
    || typeof suggested.value !== 'string' || !suggested.value.trim() || suggested.value.length > 128
    || typeof suggested.reason !== 'string' || !suggested.reason.trim() || suggested.reason.length > 500
    || Object.keys(suggested).some((key) => !['parameter', 'value', 'reason'].includes(key)))) return null
  if (value.fields.some((field) => { const entry = object(field); return !entry || typeof entry.label !== 'string' || !entry.label
    || entry.label.length > 96 || typeof entry.value !== 'string' || entry.value.length > 2000 })) return null
  return value as unknown as CapabilityConfirmation
}

export function capabilityConfirmationOutcome(tools: ConversationToolTrace[], hook: string): 'completed' | 'accepted' | 'cancelled' | 'expired' | null {
  for (const trace of tools) {
    if (trace.name !== hook) continue
    const receipt = parseCapabilityReceipt(trace.result.receipt)
    const expected = ({ save_feedback_case: 'feedback_case', start_data_loop: 'evaluation_run', propose_bundle_candidate: 'candidate' } as Record<string, string>)[hook]
    if (hook === 'start_data_loop' && trace.status === 'completed' && receipt?.entity_type === expected) {
      if (trace.result.workflow_accepted !== false && receipt.entity_id) return 'completed'
      const run = object(trace.result.run)
      if (trace.result.admission_accepted === true && parseAdmission(trace.result.admission) && dataLoopWorkflowId(run?.workflow_id)) return 'accepted'
    } else if (trace.status === 'completed' && receipt?.entity_type === expected && receipt.entity_id) return 'completed'
    if (trace.result.confirmation_outcome === 'cancelled') return 'cancelled'
    if (trace.result.confirmation_outcome === 'expired') return 'expired'
  }
  return null
}

export function capabilityConfirmationInput(inputs: ConversationInput[] | undefined, confirmation: CapabilityConfirmation): boolean {
  return Array.isArray(inputs) && inputs.some((input) => input?.request_id === confirmation.input_request_id && ['pending', 'applied'].includes(input.status))
}
