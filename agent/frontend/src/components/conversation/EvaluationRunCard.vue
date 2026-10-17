<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import CapabilityReceiptCard from '@/components/conversation/CapabilityReceiptCard.vue'
import AdmissionStatus from '@/components/conversation/AdmissionStatus.vue'
import { useEvaluationPolling } from '@/composables/useEvaluationPolling'
import { dataLoopWorkflowId, parseCapabilityReceipt, safeReceiptLink } from '@/utils/capabilityReceipts'
import type { EvaluationSnapshot } from '@/utils/capabilityReceipts'
import type { ExecutionSnapshot } from '@/utils/admission'

const props = defineProps<{ receipt: unknown; snapshot: EvaluationSnapshot | null; execution?: ExecutionSnapshot }>()
const receipt = computed(() => parseCapabilityReceipt(props.receipt))
const snapshot = computed(() => receipt.value?.entity_type === 'evaluation_run' && props.snapshot && (
  receipt.value.entity_id === props.snapshot.workflow_id || receipt.value.status === 'not_landed'
    && props.execution?.admissionAccepted === true && props.execution.admission && dataLoopWorkflowId(props.snapshot.workflow_id)) ? props.snapshot : null)
const { state, refresh } = useEvaluationPolling(snapshot, computed(() => receipt.value?.created_at), computed(() => props.execution))
const latest = computed(() => state.value.value?.run ?? null)
const latestExecution = computed(() => state.value.value?.execution)
const liveLink = computed(() => {
  const current = state.value.value?.receipt
  return receipt.value?.status === 'not_landed' && latestExecution.value?.workflowAccepted === true && current ? safeReceiptLink(current) : null
})
const gate = (value: boolean | null) => value === null ? '未判定' : value ? '通过' : '未通过'
const phaseLabel = (phase: string) => ({
  created: '准备评测', accepted: '评测已受理', submitted: '启动请求已提交', initializing: '准备启动',
  queued: '排队中', held: '排队中', granted: '等待启动', starting: '正在启动', running: '评测进行中',
  freeze_dataset: '准备评测数据', analyze_dataset: '分析案例', attribute_errors: '分析错误原因',
  evaluate_candidate: '评测候选', record_promotion_decision: '记录审核结果', activate_bundle: '正在激活',
  waiting_approval: '评测完成 · 待人工审批', evaluation_failed: '评测未通过', approval_expired: '人工审批已过期',
  rejected: '已拒绝', activated: '已激活', terminal: '任务已结束', completed: '任务已完成', failed: '启动失败',
  unknown: '状态待核对', not_found: '状态待核对',
} as Record<string, string>)[phase] ?? '状态待核对'
</script>

<template>
  <CapabilityReceiptCard :receipt="receipt ?? props.receipt" :state-shown="!!snapshot">
    <template v-if="receipt?.status === 'not_landed' && execution?.admission?.state === 'rejected' && execution.workflowAccepted === false && execution.admissionAccepted === false">
      <p><strong>评测启动请求已拒绝，任务尚未启动。</strong></p>
      <AdmissionStatus :execution="execution" />
    </template>
    <template v-if="snapshot">
      <div class="na-evaluation-latest" aria-live="polite">
        <strong>当前最新状态</strong>
        <p v-if="state.degraded" role="status">状态未知 · 连续读取失败，自动刷新已暂停。</p>
        <template v-else-if="latest">
          <AdmissionStatus v-if="latestExecution && latestExecution.workflowAccepted !== true && (latestExecution.admission || latestExecution.workflowAccepted !== null)" :execution="latestExecution" />
          <p v-if="!latestExecution?.admission || latestExecution.workflowAccepted === true || latest.phase !== latestExecution.admission.state">{{ phaseLabel(latest.phase) }}<template v-if="latest.gate_passed !== null"> · 门禁{{ gate(latest.gate_passed) }}</template></p>
          <p v-if="latest.waiting_for_approval" class="na-muted">请进入评测详情完成人工审批。</p>
          <details class="na-evaluation-details">
            <summary>运行信息</summary>
            <p>评测行 ID：{{ latest.evaluation_run_id ?? '尚未生成' }}</p>
            <p>数据集 ID：{{ latest.dataset_id ?? '尚未生成' }}</p>
            <p>运行阶段：{{ latest.phase }}</p>
          </details>
        </template>
        <template v-else>
          <AdmissionStatus v-if="execution?.admission || execution?.workflowAccepted !== undefined && execution?.workflowAccepted !== null" :execution="execution!" />
          <p v-else-if="snapshot.phase !== 'queued'"><strong>评测已受理</strong></p>
          <p class="na-muted">{{ state.loading ? '正在读取…' : '当前状态尚未核对，可刷新重试。' }}</p>
        </template>
        <button class="na-btn na-evaluation-refresh" type="button" :disabled="state.loading" @click="refresh">{{ state.loading ? '刷新中…' : '刷新状态' }}</button>
        <RouterLink v-if="liveLink" class="na-btn" :to="liveLink">查看评测详情 →</RouterLink>
      </div>
      <details class="na-evaluation-snapshot na-evaluation-details">
        <summary>当时快照</summary>
        <AdmissionStatus v-if="execution?.admission || execution?.workflowAccepted !== undefined && execution?.workflowAccepted !== null" :execution="execution!" />
        <p>{{ phaseLabel(snapshot.phase) }} · 门禁{{ gate(snapshot.gate_passed) }}</p>
        <p v-if="snapshot.waiting_for_approval">待人工审批</p>
        <p>评测策略版本：{{ snapshot.policy_version ?? '快照未提供' }}</p>
        <p>评测行 ID：{{ snapshot.evaluation_run_id ?? '尚未生成' }}</p>
      </details>
    </template>
  </CapabilityReceiptCard>
</template>

<style scoped>
.na-evaluation-snapshot, .na-evaluation-latest { margin-top: 10px; font-size: 13px; overflow-wrap: anywhere; }
.na-evaluation-details { margin-top: 10px; color: var(--na-text-muted, #667085); font-size: 12px; }
.na-evaluation-details summary { cursor: pointer; width: fit-content; }
.na-evaluation-refresh { min-height: 30px; padding: 4px 10px; font-size: 12px; }
p { margin: 8px 0; }
</style>
