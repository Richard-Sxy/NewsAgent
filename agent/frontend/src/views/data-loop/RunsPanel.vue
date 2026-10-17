<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { dataLoopApi } from '@/api'
import type {
  DataLoopSnapshotResponse,
  RecoverDataLoopActivationResponse,
  StartDataLoopRequest,
  StartDataLoopResponse,
  SubmitDataLoopDecisionRequest,
} from '@/api'
import NaJsonBlock from '@/components/NaJsonBlock.vue'
import NaStateBlock from '@/components/NaStateBlock.vue'
import { useAsyncTask } from '@/composables/useAsyncTask'
import { useConfirmStore } from '@/stores/confirm'
import { useToastStore } from '@/stores/toast'
import { describeError } from '@/utils/errors'
import { dataLoopWorkflowId } from '@/utils/capabilityReceipts'
import { newIdempotencyKey } from '@/utils/format'
import {
  firstError,
  isoDateTimeWithTimezone,
  localInputToIso,
  requiredText,
  toLocalInputValue,
} from '@/utils/validate'

const toast = useToastStore()
const route = useRoute()
const dialog = useConfirmStore()
const { pending, run } = useAsyncTask()

const DAY_MS = 24 * 60 * 60 * 1000

/* ---------------- 启动运行 ---------------- */

const windowStart = ref(toLocalInputValue(new Date(Date.now() - 7 * DAY_MS)))
const windowEnd = ref(toLocalInputValue(new Date()))
const datasetName = ref('')
const datasetVersion = ref('')
const goldenDatasetId = ref('')
const highRiskDatasetId = ref('')
const candidateId = ref('')
const previousCandidateId = ref('')
const policyVersion = ref('')

const startResult = ref<StartDataLoopResponse | null>(null)

async function submitStart(): Promise<void> {
  const startIso = localInputToIso(windowStart.value)
  const endIso = localInputToIso(windowEnd.value)

  const problem = firstError(
    isoDateTimeWithTimezone(startIso, '窗口开始时间'),
    isoDateTimeWithTimezone(endIso, '窗口结束时间'),
    requiredText(datasetName.value, '数据集名称'),
    requiredText(datasetVersion.value, '数据集版本'),
    requiredText(goldenDatasetId.value, '黄金数据集 ID'),
    requiredText(highRiskDatasetId.value, '高风险回归数据集 ID'),
    requiredText(candidateId.value, '候选配置 ID'),
    requiredText(policyVersion.value, '评估策略版本'),
  )
  if (problem) {
    toast.warn(problem)
    return
  }
  if (new Date(startIso).getTime() >= new Date(endIso).getTime()) {
    toast.warn('窗口开始时间必须早于结束时间')
    return
  }

  const body: StartDataLoopRequest = {
    window_start: startIso,
    window_end: endIso,
    dataset_name: datasetName.value.trim(),
    dataset_version: datasetVersion.value.trim(),
    golden_dataset_id: goldenDatasetId.value.trim(),
    high_risk_regression_dataset_id: highRiskDatasetId.value.trim(),
    candidate_id: candidateId.value.trim(),
    previous_experiment_candidate_id: previousCandidateId.value.trim() || null,
    evaluation_policy_version: policyVersion.value.trim(),
    idempotency_key: newIdempotencyKey('dataloop-run'),
  }

  const result = await run(() => dataLoopApi.startRun(body), {
    success: '评测运行已受理',
    failure: '启动评测运行失败',
  })
  if (!result) return
  startResult.value = result
  workflowId.value = result.workflow_id
}

/* ---------------- 快照 / 决策 / 激活恢复 ---------------- */

const workflowId = ref('')
const snapshot = ref<DataLoopSnapshotResponse | null>(null)
const snapshotLoading = ref(false)
const snapshotError = ref<string | null>(null)
let snapshotRequest = 0
let disposed = false
const analysisReport = ref<Record<string, unknown> | null>(null)
const analysisLoading = ref(false)
const analysisError = ref<string | null>(null)

const phaseLabels: Record<string, string> = {
  created: '已受理',
  freeze_dataset: '准备评测数据',
  analyze_dataset: '分析评测数据',
  attribute_errors: '分析问题原因',
  evaluate_candidate: '评测中',
  waiting_approval: '待人工批准',
  record_promotion_decision: '保存审批结果',
  activate_bundle: '激活中',
  activated: '已激活',
  evaluation_failed: '评测未通过',
  approval_expired: '审批已过期',
  rejected: '已驳回',
}
const phaseLabel = computed(() => snapshot.value ? phaseLabels[snapshot.value.phase] ?? '状态待确认' : '')
const canDecide = computed(() => snapshot.value?.waiting_for_approval && snapshot.value.gate_passed === true)

const analysisStatusLabels = {
  completed: '完成',
  degraded: '未完整完成',
  disabled: '本次未启用',
}

async function loadAnalysisReport(): Promise<void> {
  if (!snapshot.value?.dataset_analysis_artifact_uri) return
  analysisLoading.value = true
  analysisError.value = null
  const id = snapshot.value.workflow_id
  try {
    const report = await dataLoopApi.datasetAnalysis(id)
    if (snapshot.value?.workflow_id === id) analysisReport.value = report
  } catch (cause) {
    if (snapshot.value?.workflow_id === id) analysisError.value = describeError(cause)
  } finally {
    analysisLoading.value = false
  }
}

const decisionAction = ref<'approve' | 'reject'>('approve')
const decisionReason = ref('')

const recovery = ref<RecoverDataLoopActivationResponse | null>(null)

async function loadSnapshot(): Promise<boolean> {
  const id = workflowId.value.trim()
  if (!id) {
    toast.warn('请先填写工作流 ID')
    return false
  }
  snapshotLoading.value = true
  const sequence = ++snapshotRequest
  snapshotError.value = null
  analysisReport.value = null
  analysisError.value = null
  try {
    const response = await dataLoopApi.snapshot(id)
    if (disposed || sequence !== snapshotRequest || workflowId.value.trim() !== id) return false
    if (response.workflow_id !== id) throw new Error('评测快照响应与工作流 ID 不一致')
    snapshot.value = response
    return true
  } catch (cause) {
    if (!disposed && sequence === snapshotRequest) { snapshotError.value = describeError(cause); snapshot.value = null }
    return false
  } finally {
    if (!disposed && sequence === snapshotRequest) snapshotLoading.value = false
  }
}

watch(() => route.query.workflow_id, (value) => {
  if (value === undefined) return
  ++snapshotRequest
  snapshot.value = null
  snapshotError.value = null
  snapshotLoading.value = false
  workflowId.value = ''
  const id = dataLoopWorkflowId(value)
  if (!id) { snapshotError.value = '链接中的 Workflow ID 不合法。'; return }
  workflowId.value = id
  void loadSnapshot()
}, { immediate: true })
onBeforeUnmount(() => { disposed = true; ++snapshotRequest })

async function submitRunDecision(): Promise<void> {
  const id = workflowId.value.trim()
  if (!id) {
    toast.warn('请先填写工作流 ID')
    return
  }
  const problem = requiredText(decisionReason.value, '决策原因')
  if (problem) {
    toast.warn(problem)
    return
  }

  // Always refresh before submitting. This prevents a stale/closed workflow
  // from producing a misleading 503 and tells the operator which phase is
  // actionable.
  if (!(await loadSnapshot()) || !snapshot.value) return
  if (!snapshot.value.waiting_for_approval || snapshot.value.gate_passed !== true) {
    toast.warn(
      `当前阶段为“${snapshot.value.phase}”，门禁未处于人工审批阶段，暂不能提交决策`,
    )
    return
  }

  const verb = decisionAction.value === 'approve' ? '通过' : '驳回'
  const confirmed = await dialog.ask({
    title: `确认${verb}该评测运行？`,
    description: decisionAction.value === 'approve'
      ? `评测 ${id} 批准后将继续激活本次候选配置，请先核对评测证据。`
      : `评测 ${id} 将被驳回，本次候选配置不会激活。`,
    confirmText: `确认${verb}`,
    danger: decisionAction.value === 'reject',
  })
  if (!confirmed) return

  const body: SubmitDataLoopDecisionRequest = {
    action: decisionAction.value,
    reason: decisionReason.value.trim(),
    idempotency_key: newIdempotencyKey('dataloop-decision'),
  }

  const result = await run(() => dataLoopApi.decideRun(id, body), {
    success: `已提交${verb}`,
    failure: '提交门禁决策失败',
  })
  if (!result) return
  decisionReason.value = ''
  await loadSnapshot()
}

async function recoverActivation(): Promise<void> {
  const id = workflowId.value.trim()
  if (!id) {
    toast.warn('请先填写工作流 ID')
    return
  }

  const confirmed = await dialog.ask({
    title: '确认执行激活恢复？',
    description: `将针对工作流 ${id} 创建一条恢复工作流。这是写操作，会产生新的工作流实例。`,
    confirmText: '确认恢复',
    danger: true,
  })
  if (!confirmed) return

  const result = await run(
    () =>
      dataLoopApi.recoverActivation(id, {
        idempotency_key: newIdempotencyKey('dataloop-recover'),
      }),
    { success: '激活恢复已受理', failure: '激活恢复失败' },
  )
  if (result) recovery.value = result
}
</script>

<template>
  <section class="na-card">
    <div class="na-card__head">
      <h2>评测结果</h2>
      <button v-if="workflowId" class="na-btn" type="button" :disabled="snapshotLoading" @click="loadSnapshot">
        {{ snapshotLoading ? '刷新中…' : '刷新' }}
      </button>
    </div>

    <details class="na-disclosure" :open="!workflowId">
      <summary>查找评测</summary>
      <div class="na-row">
        <div class="na-field na-field--grow">
          <label for="workflow-id">评测编号</label>
          <input id="workflow-id" v-model="workflowId" class="na-input" placeholder="粘贴评测编号" autocomplete="off" @keyup.enter="loadSnapshot">
        </div>
        <div class="na-field na-field--action">
          <button class="na-btn" type="button" :disabled="snapshotLoading" @click="loadSnapshot">查询</button>
        </div>
      </div>
    </details>

    <NaStateBlock :loading="snapshotLoading" :error="snapshotError" :empty="!snapshot" empty-text="从对话中的评测卡片进入，或填写编号查询。" loading-text="读取评测结果…">
      <template v-if="snapshot">
        <div class="na-metrics">
          <div class="na-metric">
            <div class="na-metric__label">当前状态</div>
            <div class="na-metric__value">{{ phaseLabel }}</div>
          </div>
          <div class="na-metric">
            <div class="na-metric__label">评测门禁</div>
            <div class="na-metric__value">{{ snapshot.gate_passed === null ? '尚未判定' : snapshot.gate_passed ? '通过' : '未通过' }}</div>
          </div>
        </div>
        <p v-if="canDecide" class="na-notice">评测已通过，请核对完整证据后决定是否批准。批准后将继续激活候选配置。</p>
        <p v-else class="na-muted">当前阶段无需人工审批。</p>

        <details class="na-disclosure">
          <summary>查看评测证据与运行详情</summary>
          <dl class="na-defs">
            <div><dt>评测编号</dt><dd class="na-mono">{{ snapshot.workflow_id }}</dd></div>
            <div><dt>评测记录</dt><dd class="na-mono">{{ snapshot.evaluation_run_id ?? '尚未生成' }}</dd></div>
            <div><dt>冻结数据集</dt><dd class="na-mono">{{ snapshot.dataset_id ?? '尚未生成' }}</dd></div>
          </dl>
          <h3>冻结案例分析</h3>
          <p class="na-muted">仅统计案例所引用运行的完整榜单，不代表全站效果。</p>
          <p>分析状态：{{ snapshot.dataset_analysis_status ? analysisStatusLabels[snapshot.dataset_analysis_status] : '尚无分析记录' }}</p>
          <p v-if="snapshot.dataset_analysis_summary?.case_count !== undefined">已覆盖案例：{{ snapshot.dataset_analysis_summary.covered_case_count }} / {{ snapshot.dataset_analysis_summary.case_count }}</p>
          <p v-if="snapshot.dataset_analysis_summary?.source_run_count !== undefined">已分析来源运行：{{ snapshot.dataset_analysis_summary.analyzed_run_count }} / {{ snapshot.dataset_analysis_summary.source_run_count }}</p>
          <button v-if="snapshot.dataset_analysis_artifact_uri" class="na-btn" type="button" :disabled="analysisLoading" @click="loadAnalysisReport">{{ analysisLoading ? '读取报告…' : '读取分析报告' }}</button>
          <NaStateBlock v-if="analysisError" :error="analysisError" />
          <NaJsonBlock v-if="analysisReport" :value="analysisReport" label="完整分析报告" />
          <NaJsonBlock :value="snapshot" label="完整评测快照" />
        </details>

        <div v-if="canDecide" class="na-panel">
          <h3>人工审批</h3>
          <div class="na-row">
            <div class="na-field na-field--fixed">
              <label for="run-decision">审批结果</label>
              <select id="run-decision" v-model="decisionAction" class="na-select">
                <option value="approve">批准</option>
                <option value="reject">驳回</option>
              </select>
            </div>
            <div class="na-field na-field--grow">
              <label for="run-reason">原因</label>
              <input id="run-reason" v-model="decisionReason" class="na-input" placeholder="填写审批原因">
            </div>
            <div class="na-field na-field--action">
              <button class="na-btn na-btn--primary" type="button" :disabled="pending" @click="submitRunDecision">提交审批</button>
            </div>
          </div>
        </div>
      </template>
    </NaStateBlock>
  </section>

  <details class="na-card na-disclosure">
    <summary>高级操作：手动启动评测</summary>
    <p class="na-muted">日常测试可在对话中启动评测。这里保留完整配置入口。</p>
    <div class="na-row">
      <div class="na-field na-field--fixed">
        <label for="window-start">窗口开始</label>
        <input id="window-start" v-model="windowStart" class="na-input" type="datetime-local">
      </div>
      <div class="na-field na-field--fixed">
        <label for="window-end">窗口结束</label>
        <input id="window-end" v-model="windowEnd" class="na-input" type="datetime-local">
      </div>
      <div class="na-field na-field--fixed">
        <label for="dataset-name">数据集名称</label>
        <input id="dataset-name" v-model="datasetName" class="na-input" autocomplete="off">
      </div>
      <div class="na-field na-field--fixed">
        <label for="dataset-version">数据集版本</label>
        <input id="dataset-version" v-model="datasetVersion" class="na-input" autocomplete="off">
      </div>
    </div>

    <div class="na-row">
      <div class="na-field na-field--fixed">
        <label for="golden-id">黄金数据集 ID</label>
        <input id="golden-id" v-model="goldenDatasetId" class="na-input" autocomplete="off">
      </div>
      <div class="na-field na-field--fixed">
        <label for="risk-id">高风险回归数据集 ID</label>
        <input id="risk-id" v-model="highRiskDatasetId" class="na-input" autocomplete="off">
      </div>
      <div class="na-field na-field--fixed">
        <label for="candidate-id">候选配置 ID</label>
        <input id="candidate-id" v-model="candidateId" class="na-input" autocomplete="off">
      </div>
      <div class="na-field na-field--fixed">
        <label for="previous-candidate">上一个候选 ID（可选）</label>
        <input id="previous-candidate" v-model="previousCandidateId" class="na-input" autocomplete="off">
      </div>
      <div class="na-field na-field--fixed">
        <label for="policy-version">评估策略版本</label>
        <input id="policy-version" v-model="policyVersion" class="na-input" autocomplete="off">
      </div>
    </div>

    <div class="na-row na-row--end">
      <button class="na-btn na-btn--primary" type="button" :disabled="pending" @click="submitStart">
        {{ pending ? '提交中…' : '启动运行' }}
      </button>
    </div>

    <details v-if="startResult" class="na-disclosure"><summary>启动结果详情</summary><NaJsonBlock :value="startResult" label="启动结果" /></details>
  </details>

  <details v-if="workflowId" class="na-card na-disclosure">
    <summary>故障处理：激活恢复</summary>
    <p class="na-muted">仅在激活失败时使用。恢复会创建新的工作流，需要再次确认。</p>
    <button class="na-btn na-btn--danger" type="button" :disabled="pending" @click="recoverActivation">激活恢复</button>
    <NaJsonBlock v-if="recovery" :value="recovery" label="激活恢复结果" />
  </details>
</template>
