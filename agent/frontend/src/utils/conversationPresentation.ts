import type { ConversationToolTrace, ConversationTurn } from '@/api/conversations'
import { toolReceipts } from '@/utils/capabilityReceipts'
import { writingConfirmation } from '@/utils/writingConfirmation'
import { capabilityConfirmation } from '@/utils/capabilityConfirmation'

const CARD_ENTITIES: Record<string, string> = {
  list_writing_jobs: 'writing_job',
  writing_job_status: 'writing_job',
  start_writing_intent: 'writing_job',
  save_short_term_memory: 'memory',
  propose_long_term_memory: 'memory',
  memory_status: 'memory',
  save_feedback_case: 'feedback_case',
  start_data_loop: 'evaluation_run',
  evaluation_status: 'evaluation_run',
}
const MEMORY_STATES = new Set(['active', 'pending', 'approved', 'rejected', 'expired', 'superseded', 'invalidated'])
const FEEDBACK_STATES = new Set(['collected', 'needs_label', 'labeled', 'excluded', 'frozen'])
const EVALUATION_PHASES = new Set(['created', 'accepted', 'starting', 'freeze_dataset', 'analyze_dataset',
  'attribute_errors', 'evaluate_candidate', 'waiting_approval', 'record_promotion_decision', 'activate_bundle', 'activated'])

function representedByCard(trace: ConversationToolTrace): boolean {
  const entity = CARD_ENTITIES[trace.name]
  if (!entity || trace.status !== 'completed' || trace.error_code
    || !trace.result || typeof trace.result !== 'object' || Array.isArray(trace.result)) return false

  // Confirmation cards carry their own proposed values and side effects. Other
  // not-landed results still need the original explanation in the message body.
  if (writingConfirmation(trace) || capabilityConfirmation(trace)) return true
  if (Object.hasOwn(trace.result, 'confirmation')) return false

  const receipts = toolReceipts(trace)
  return receipts.length > 0 && receipts.every((item) => {
    if (!item.receipt || item.receipt.entity_type !== entity || item.receipt.status === 'not_landed'
      || item.execution.admission?.state === 'unknown' || item.execution.admission?.state === 'not_found') return false
    switch (entity) {
      case 'writing_job': return item.job !== null && !['failed', 'cancelled'].includes(item.job.status)
      case 'memory': return item.memory !== null && MEMORY_STATES.has(item.memory.effective_status ?? item.memory.status)
      case 'feedback_case': return item.feedback !== null && FEEDBACK_STATES.has(item.feedback.status)
      case 'evaluation_run': return item.run !== null && EVALUATION_PHASES.has(item.run.phase)
      default: return false
    }
  })
}

/** Fold duplicate prose only when every saved result has a complete capability
 * card. Keep the original answer unchanged and accessible in a closed details.
 * Candidate cards lack their business snapshot, so they remain prose-first.
 */
export function preferCapabilityCards(turn: Pick<ConversationTurn, 'status' | 'error_code' | 'tools'>): boolean {
  return turn.status === 'completed' && !turn.error_code && Array.isArray(turn.tools)
    && turn.tools.length > 0 && turn.tools.every(representedByCard)
}
