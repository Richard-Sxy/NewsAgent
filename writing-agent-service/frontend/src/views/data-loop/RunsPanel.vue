<script setup lang="ts">
import { ref } from 'vue'

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
import { newIdempotencyKey } from '@/utils/format'
import {
  firstError,
  isoDateTimeWithTimezone,
  localInputToIso,
  requiredText,
  toLocalInputValue,
} from '@/utils/validate'

const toast = useToastStore()
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
  snapshotError.value = null
  try {
    snapshot.value = await dataLoopApi.snapshot(id)
    return true
  } catch (cause) {
    snapshotError.value = describeError(cause)
    snapshot.value = null
    return false
  } finally {
    snapshotLoading.value = false
  }
}

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
    description: `工作流 ${id} 的门禁决策会写入数据集冻结链路。`,
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
      <h2>启动评测运行</h2>
      <span class="na-badge na-badge--live">POST /data-loop/runs</span>
    </div>

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

    <NaJsonBlock v-if="startResult" :value="startResult" label="启动结果" />
  </section>

  <section class="na-card">
    <div class="na-card__head">
      <h2>快照与门禁</h2>
      <span class="na-badge">GET /runs/{'{workflow_id}'}</span>
    </div>

    <div class="na-row">
      <div class="na-field na-field--grow">
        <label for="workflow-id">工作流 ID</label>
        <input
          id="workflow-id"
          v-model="workflowId"
          class="na-input"
          placeholder="从启动结果或上游系统获取"
          autocomplete="off"
          @keyup.enter="loadSnapshot"
        >
      </div>
      <div class="na-field na-field--action">
        <button class="na-btn" type="button" :disabled="snapshotLoading" @click="loadSnapshot">
          查询快照
        </button>
      </div>
      <div class="na-field na-field--action">
        <button class="na-btn na-btn--danger" type="button" :disabled="pending" @click="recoverActivation">
          激活恢复
        </button>
      </div>
    </div>

    <NaStateBlock
      :loading="snapshotLoading"
      :error="snapshotError"
      :empty="!snapshot"
      empty-text="尚未查询快照"
      loading-text="查询快照…"
    >
      <template v-if="snapshot">
        <div class="na-metrics">
          <div class="na-metric">
            <div class="na-metric__label">阶段</div>
            <div class="na-metric__value">{{ snapshot.phase }}</div>
          </div>
          <div class="na-metric">
            <div class="na-metric__label">门禁</div>
            <div class="na-metric__value">
              {{ snapshot.gate_passed === null ? '未判定' : snapshot.gate_passed ? '通过' : '未通过' }}
            </div>
          </div>
          <div class="na-metric">
            <div class="na-metric__label">等待审批</div>
            <div class="na-metric__value">{{ snapshot.waiting_for_approval ? '是' : '否' }}</div>
          </div>
          <div class="na-metric">
            <div class="na-metric__label">数据集</div>
            <div class="na-metric__value na-mono">{{ snapshot.dataset_id ?? '-' }}</div>
          </div>
        </div>
        <NaJsonBlock :value="snapshot" label="快照原始载荷" />
      </template>
    </NaStateBlock>

    <h3>门禁决策</h3>
    <div class="na-row">
      <div class="na-field na-field--fixed">
        <label for="run-decision">动作</label>
        <select id="run-decision" v-model="decisionAction" class="na-select">
          <option value="approve">通过</option>
          <option value="reject">驳回</option>
        </select>
      </div>
      <div class="na-field na-field--grow">
        <label for="run-reason">原因</label>
        <input id="run-reason" v-model="decisionReason" class="na-input">
      </div>
      <div class="na-field na-field--action">
        <button class="na-btn na-btn--primary" type="button" :disabled="pending" @click="submitRunDecision">
          提交门禁决策
        </button>
      </div>
    </div>

    <NaJsonBlock v-if="recovery" :value="recovery" label="激活恢复结果" />
  </section>
</template>
