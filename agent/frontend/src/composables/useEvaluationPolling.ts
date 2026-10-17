import { onBeforeUnmount, ref, watch } from 'vue'
import type { Ref } from 'vue'

import { conversationsApi } from '@/api/conversations'
import { createEntityPoller } from '@/utils/entityPolling'
import type { EntityPollState, EntitySubscription } from '@/utils/entityPolling'
import { parseCapabilityReceipt, parseEvaluationSnapshot } from '@/utils/capabilityReceipts'
import type { CapabilityReceipt, EvaluationSnapshot } from '@/utils/capabilityReceipts'
import { admissionTerminal, executionSnapshot } from '@/utils/admission'
import type { ExecutionSnapshot } from '@/utils/admission'

export const EVALUATION_POLL_INTERVAL_MS = 15_000
interface ObservedRun { run: EvaluationSnapshot; observedAt: number; execution: ExecutionSnapshot; receipt: CapabilityReceipt | null }
// Policies come from immutable server receipts; snapshot API doesn't expose policy versions.
const evaluationPoller = createEntityPoller<ObservedRun>({
  intervalMs: EVALUATION_POLL_INTERVAL_MS,
  read: async (id, seed) => {
    const response = await conversationsApi.evaluationReceipt(id)
    const snapshot = parseEvaluationSnapshot(response.run, id, seed.run.policy_version ?? undefined)
    const receipt = parseCapabilityReceipt(response.receipt)
    const execution = executionSnapshot(response)
    const queued = receipt?.status === 'not_landed' && execution.admissionAccepted === true && execution.admission !== null
    // This authenticated read route verifies the persisted request's tenant even
    // when its worker query is unavailable. It grants no business acceptance.
    const unknown = receipt?.status === 'not_landed' && snapshot?.phase === 'unknown'
      && snapshot.gate_passed === null && snapshot.waiting_for_approval === false && snapshot.evaluation_run_id === null
      && snapshot.dataset_id === null && snapshot.terminal === false
      && response.workflow_accepted === null && response.admission_accepted === null && execution.admission?.state === 'unknown'
      && execution.admission.queue_position === null && execution.admission.active_count === null && execution.admission.budget === null
      && execution.admission.bootstrap_complete === null
    if (!snapshot || receipt?.entity_type !== 'evaluation_run' || !(receipt.entity_id === id || queued || unknown)) throw new Error('Unexpected Data Loop response')
    return { run: snapshot, observedAt: Date.parse(receipt.created_at), execution, receipt }
  },
  terminal: (value) => value.run.terminal || admissionTerminal(value.execution.admission),
  newer: (candidate, current) => candidate.observedAt > current.observedAt,
})

export function useEvaluationPolling(snapshot: Ref<EvaluationSnapshot | null>, snapshotAt: Ref<string | undefined>, execution: Ref<ExecutionSnapshot | undefined>) {
  const state = ref<EntityPollState<ObservedRun>>({ value: null, loading: false, degraded: false, error: null })
  let subscription: EntitySubscription | null = null
  watch(snapshot, (run) => {
    subscription?.unsubscribe()
    subscription = null
    state.value = { value: null, loading: false, degraded: false, error: null }
    if (run) {
      subscription = evaluationPoller.subscribe(run.workflow_id, { run, observedAt: Date.parse(snapshotAt.value ?? '') || Date.now(),
        execution: execution.value ?? executionSnapshot(null), receipt: null },
        (value) => { state.value = value })
    }
  }, { immediate: true })
  onBeforeUnmount(() => { subscription?.unsubscribe(); subscription = null })
  return { state, refresh: () => subscription?.refresh() }
}
