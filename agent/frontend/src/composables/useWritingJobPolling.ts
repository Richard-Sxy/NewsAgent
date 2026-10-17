import { onBeforeUnmount, ref, watch } from 'vue'
import type { Ref } from 'vue'

import { conversationsApi } from '@/api/conversations'
import { createEntityPoller } from '@/utils/entityPolling'
import type { EntityPollState, EntitySubscription } from '@/utils/entityPolling'
import { parseCapabilityReceipt, parseWritingJobSnapshot, writingJobGate, writingJobTerminal } from '@/utils/capabilityReceipts'
import type { WritingJobSnapshot } from '@/utils/capabilityReceipts'
import { admissionTerminal, executionSnapshot } from '@/utils/admission'
import type { ExecutionSnapshot } from '@/utils/admission'

export interface LiveWritingJob {
  job: WritingJobSnapshot
  pendingGate: string | null
  workflowAvailable: boolean
  execution: ExecutionSnapshot
  observedAt: number
}

const writingPoller = createEntityPoller<LiveWritingJob>({
  read: async (id) => {
    const result = await conversationsApi.writingReceipt(id)
    const snapshot = parseWritingJobSnapshot(result.job, id)
    const receipt = parseCapabilityReceipt(result.receipt)
    if (!snapshot || receipt?.entity_type !== 'writing_job' || receipt.entity_id !== id) throw new Error('Unexpected writing job response')
    return { job: snapshot, pendingGate: receipt.pending_gate ?? writingJobGate(snapshot),
      workflowAvailable: result.workflow_available !== false, execution: executionSnapshot(result),
      observedAt: Date.parse(receipt.created_at) }
  },
  terminal: (value) => writingJobTerminal(value.job) || admissionTerminal(value.execution.admission),
  newer: (candidate, current) => candidate.observedAt > current.observedAt,
})

export function useWritingJobPolling(snapshot: Ref<WritingJobSnapshot | null>, gate: Ref<string | null>,
  execution: Ref<ExecutionSnapshot | undefined>, snapshotAt: Ref<string | undefined>) {
  const state = ref<EntityPollState<LiveWritingJob>>({ value: null, loading: false, degraded: false, error: null })
  let subscription: EntitySubscription | null = null
  watch(snapshot, (job) => {
    subscription?.unsubscribe()
    subscription = null
    state.value = { value: null, loading: false, degraded: false, error: null }
    if (job) subscription = writingPoller.subscribe(job.id, { job, pendingGate: gate.value, workflowAvailable: true,
      execution: execution.value ?? executionSnapshot(null), observedAt: Date.parse(snapshotAt.value ?? job.updated_at) },
      (value) => { state.value = value })
  }, { immediate: true })
  onBeforeUnmount(() => { subscription?.unsubscribe(); subscription = null })
  return { state, refresh: () => subscription?.refresh() }
}
