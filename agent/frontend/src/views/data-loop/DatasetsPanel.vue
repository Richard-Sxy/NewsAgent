<script setup lang="ts">
import { ref } from 'vue'

import { dataLoopApi } from '@/api'
import type {
  EvaluationDatasetLayer,
  FreezeDatasetRequest,
  FreezeDatasetResponse,
} from '@/api'
import NaJsonBlock from '@/components/NaJsonBlock.vue'
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
  toLineList,
  toLocalInputValue,
} from '@/utils/validate'

const toast = useToastStore()
const dialog = useConfirmStore()
const { pending, run } = useAsyncTask()

const LAYERS: Array<{ value: EvaluationDatasetLayer; label: string }> = [
  { value: 'golden', label: '黄金集（golden）' },
  { value: 'fresh_bad_case', label: '新鲜坏例（fresh_bad_case）' },
  { value: 'high_risk_regression', label: '高风险回归（high_risk_regression）' },
]

const datasetName = ref('')
const datasetVersion = ref('')
const datasetLayer = ref<EvaluationDatasetLayer>('golden')
const description = ref('')
const caseIdsText = ref('')
const sourceCutoff = ref(toLocalInputValue(new Date()))

const freezeResult = ref<FreezeDatasetResponse | null>(null)

const lookupId = ref('')
const lookupResult = ref<Record<string, unknown> | null>(null)
const lookupLoading = ref(false)
const lookupError = ref<string | null>(null)
const prefillLoading = ref(false)

async function loadLabeledCases(): Promise<void> {
  prefillLoading.value = true
  try {
    const page = await dataLoopApi.listFeedbackCases({ status: ['labeled'], limit: 50 })
    if (page.cases.length === 0) {
      toast.info('没有状态为「已标注」的反馈案例可带入')
      return
    }
    const existing = toLineList(caseIdsText.value)
    const merged = new Set([...existing, ...page.cases.map((item) => item.id)])
    caseIdsText.value = [...merged].join('\n')
    toast.ok(`已带入 ${page.cases.length} 条已标注案例`)
  } catch (cause) {
    toast.fail(`载入已标注案例失败：${describeError(cause)}`)
  } finally {
    prefillLoading.value = false
  }
}

async function submitFreeze(): Promise<void> {
  const cutoffIso = localInputToIso(sourceCutoff.value)
  const caseIds = toLineList(caseIdsText.value)

  const problem = firstError(
    requiredText(datasetName.value, '数据集名称'),
    requiredText(datasetVersion.value, '数据集版本'),
    requiredText(description.value, '描述'),
    isoDateTimeWithTimezone(cutoffIso, '数据截止时间'),
    caseIds.length === 0 ? '至少需要一条反馈案例 ID' : null,
  )
  if (problem) {
    toast.warn(problem)
    return
  }

  const confirmed = await dialog.ask({
    title: '确认冻结该数据集？',
    description: `将把 ${caseIds.length} 条反馈案例冻结为 ${datasetLayer.value} 层数据集 ${datasetName.value.trim()}@${datasetVersion.value.trim()}。冻结后不可修改。`,
    confirmText: '确认冻结',
    danger: true,
  })
  if (!confirmed) return

  const body: FreezeDatasetRequest = {
    dataset_name: datasetName.value.trim(),
    dataset_version: datasetVersion.value.trim(),
    dataset_layer: datasetLayer.value,
    description: description.value.trim(),
    feedback_case_ids: caseIds,
    source_cutoff_at: cutoffIso,
    idempotency_key: newIdempotencyKey('dataset-freeze'),
  }

  const result = await run(() => dataLoopApi.freezeDataset(body), {
    success: '数据集已冻结',
    failure: '冻结数据集失败',
  })
  if (!result) return
  freezeResult.value = result
  caseIdsText.value = ''
}

async function lookup(): Promise<void> {
  const id = lookupId.value.trim()
  if (!id) {
    toast.warn('请填写数据集 ID')
    return
  }
  lookupLoading.value = true
  lookupError.value = null
  try {
    lookupResult.value = await dataLoopApi.getDataset(id)
  } catch (cause) {
    lookupError.value = describeError(cause)
    lookupResult.value = null
  } finally {
    lookupLoading.value = false
  }
}
</script>

<template>
  <div class="na-grid na-grid--detail">
    <section class="na-card">
      <div class="na-card__head">
        <h2>冻结评估数据集</h2>
        <span class="na-badge na-badge--danger">POST /data-loop/datasets/freeze</span>
      </div>

      <div class="na-row">
        <div class="na-field na-field--fixed">
          <label for="ds-name">数据集名称</label>
          <input id="ds-name" v-model="datasetName" class="na-input" autocomplete="off">
        </div>
        <div class="na-field na-field--fixed">
          <label for="ds-version">数据集版本</label>
          <input id="ds-version" v-model="datasetVersion" class="na-input" autocomplete="off">
        </div>
        <div class="na-field na-field--fixed">
          <label for="ds-layer">数据层</label>
          <select id="ds-layer" v-model="datasetLayer" class="na-select">
            <option v-for="item in LAYERS" :key="item.value" :value="item.value">
              {{ item.label }}
            </option>
          </select>
        </div>
        <div class="na-field na-field--fixed">
          <label for="ds-cutoff">数据截止时间</label>
          <input id="ds-cutoff" v-model="sourceCutoff" class="na-input" type="datetime-local">
        </div>
      </div>

      <div class="na-field">
        <label for="ds-description">描述</label>
        <input id="ds-description" v-model="description" class="na-input">
      </div>

      <div class="na-field">
        <label for="ds-cases">反馈案例 ID（每行一个）</label>
        <textarea
          id="ds-cases"
          v-model="caseIdsText"
          class="na-textarea"
          placeholder="每行一个 feedback_case_id"
        />
      </div>

      <div class="na-row na-row--end">
        <button class="na-btn" type="button" :disabled="prefillLoading" @click="loadLabeledCases">
          {{ prefillLoading ? '载入中…' : '带入已标注案例' }}
        </button>
        <button class="na-btn na-btn--danger" type="button" :disabled="pending" @click="submitFreeze">
          {{ pending ? '提交中…' : '冻结数据集' }}
        </button>
      </div>

      <NaJsonBlock v-if="freezeResult" :value="freezeResult" label="冻结结果" />
    </section>

    <section class="na-card">
      <div class="na-card__head">
        <h2>查询数据集</h2>
        <span class="na-badge">GET /datasets/{'{dataset_id}'}</span>
      </div>

      <div class="na-row">
        <div class="na-field na-field--grow">
          <label for="ds-lookup">数据集 ID</label>
          <input
            id="ds-lookup"
            v-model="lookupId"
            class="na-input"
            autocomplete="off"
            @keyup.enter="lookup"
          >
        </div>
        <div class="na-field na-field--action">
          <button class="na-btn" type="button" :disabled="lookupLoading" @click="lookup">查询</button>
        </div>
      </div>

      <p v-if="lookupError" class="na-message na-message--error">{{ lookupError }}</p>
      <NaJsonBlock
        v-if="lookupResult"
        :value="lookupResult"
        label="数据集详情"
        :collapsed-height="380"
      />
      <p v-else-if="!lookupError" class="na-muted">输入数据集 ID 后查询其清单与用例。</p>
    </section>
  </div>
</template>
