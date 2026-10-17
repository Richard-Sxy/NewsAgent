<script setup lang="ts">
import { computed } from 'vue'

import CapabilityReceiptCard from '@/components/conversation/CapabilityReceiptCard.vue'
import AdmissionStatus from '@/components/conversation/AdmissionStatus.vue'
import { useWritingJobPolling } from '@/composables/useWritingJobPolling'
import { gateLabel, parseCapabilityReceipt } from '@/utils/capabilityReceipts'
import type { WritingJobSnapshot } from '@/utils/capabilityReceipts'
import { formatDateTime, formatPercent } from '@/utils/format'
import { jobStatusLabel, stepLabel } from '@/utils/labels'
import type { ExecutionSnapshot } from '@/utils/admission'

const props = defineProps<{ receipt: unknown; snapshot: WritingJobSnapshot | null; execution?: ExecutionSnapshot }>()
const receipt = computed(() => parseCapabilityReceipt(props.receipt))
const snapshot = computed(() => receipt.value?.entity_type === 'writing_job'
  && receipt.value.entity_id === props.snapshot?.id ? props.snapshot : null)
const snapshotGate = computed(() => receipt.value?.pending_gate ?? null)
const { state, refresh } = useWritingJobPolling(snapshot, snapshotGate, computed(() => props.execution), computed(() => receipt.value?.created_at))
const latest = computed(() => state.value.value)
</script>

<template>
  <CapabilityReceiptCard :receipt="receipt ?? props.receipt" :state-shown="!!snapshot">
    <template v-if="snapshot">
      <h3 class="na-writing-card__topic">{{ snapshot.topic }}</h3>
      <div class="na-writing-card__latest" aria-live="polite">
        <strong>当前最新状态</strong>
        <p v-if="state.degraded" class="na-writing-card__unknown" role="status">状态未知 · 连续读取失败，自动刷新已暂停。</p>
        <template v-else-if="latest">
          <AdmissionStatus v-if="latest.execution && latest.execution.workflowAccepted !== true && (latest.execution.admission || latest.execution.workflowAccepted !== null)" :execution="latest.execution" />
          <p>{{ jobStatusLabel(latest.job.status) }} · {{ stepLabel(latest.job.current_step) }} · {{ formatPercent(latest.job.progress_percent) }}</p>
          <p v-if="latest.pendingGate" class="na-muted">等待人工审批：{{ gateLabel(latest.pendingGate) }}。请进入写作任务详情处理。</p>
          <p v-else-if="!latest.workflowAvailable" class="na-muted">实时状态暂不可用，以上为已保存任务状态。</p>
          <details class="na-writing-card__details"><summary>状态信息</summary><p>任务更新时间：{{ formatDateTime(latest.job.updated_at) }}</p></details>
        </template>
        <template v-else>
          <p v-if="execution?.admission?.state === 'rejected' && execution.workflowAccepted === false"><strong>任务已保存，但启动请求已拒绝。</strong></p>
          <AdmissionStatus v-if="execution?.admission || execution?.workflowAccepted !== undefined && execution?.workflowAccepted !== null" :execution="execution!" />
          <p class="na-muted">{{ state.loading ? '正在读取…' : '当前状态尚未核对，可刷新重试。' }}</p>
        </template>
        <p v-if="state.error && !state.degraded" class="na-muted">暂时无法刷新，稍后重试。</p>
        <button class="na-btn na-writing-card__refresh" type="button" :disabled="state.loading" @click="refresh">{{ state.loading ? '刷新中…' : '刷新状态' }}</button>
      </div>
      <details class="na-writing-card__snapshot na-writing-card__details">
        <summary>当时快照</summary>
        <p v-if="execution?.admission?.state === 'rejected' && execution.workflowAccepted === false"><strong>任务已保存，但启动请求已拒绝。</strong></p>
        <AdmissionStatus v-if="execution?.admission || execution?.workflowAccepted !== undefined && execution?.workflowAccepted !== null" :execution="execution!" />
        <p>{{ jobStatusLabel(snapshot.status) }} · {{ stepLabel(snapshot.current_step) }} · {{ formatPercent(snapshot.progress_percent) }}</p>
        <p v-if="snapshotGate" class="na-muted">等待人工审批：{{ gateLabel(snapshotGate) }}。</p>
      </details>
    </template>
  </CapabilityReceiptCard>
</template>

<style scoped>
.na-writing-card__topic { margin: 12px 0; font-size: 15px; overflow-wrap: anywhere; }
.na-writing-card__snapshot, .na-writing-card__latest { margin-top: 10px; font-size: 13px; }
.na-writing-card__details { color: var(--na-text-muted, #667085); font-size: 12px; }
.na-writing-card__details summary { cursor: pointer; width: fit-content; }
.na-writing-card__refresh { margin-top: 6px; min-height: 30px; padding: 4px 10px; font-size: 12px; }
p { margin: 6px 0; }
.na-writing-card__unknown { color: #805c00; }
</style>
