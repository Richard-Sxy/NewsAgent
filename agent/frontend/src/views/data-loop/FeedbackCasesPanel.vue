<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { dataLoopApi } from '@/api'
import type {
  FeedbackCase,
  FeedbackStatus,
  JsonObject,
  ReviewFeedbackLabelRequest,
  SubmitFeedbackLabelRequest,
} from '@/api'
import NaJsonBlock from '@/components/NaJsonBlock.vue'
import NaStateBlock from '@/components/NaStateBlock.vue'
import { useAsyncTask } from '@/composables/useAsyncTask'
import { useConfirmStore } from '@/stores/confirm'
import { useToastStore } from '@/stores/toast'
import { describeError } from '@/utils/errors'
import { formatDateTime, newIdempotencyKey } from '@/utils/format'
import {
  feedbackSourceLabel,
  feedbackStatusLabel,
  feedbackStatusTone,
  problemTypeLabel,
  severityLabel,
  severityTone,
} from '@/utils/labels'
import { jsonText, parseJson } from '@/utils/validate'

const toast = useToastStore()
const dialog = useConfirmStore()
const { pending, run } = useAsyncTask()

const STATUS_OPTIONS: FeedbackStatus[] = [
  'collected',
  'needs_label',
  'labeled',
  'excluded',
  'frozen',
]

const cases = ref<FeedbackCase[]>([])
const listLoading = ref(false)
const listError = ref<string | null>(null)
const statusFilter = ref<FeedbackStatus | ''>('')

const selectedId = ref<string | null>(null)
const selected = computed(() => cases.value.find((item) => item.id === selectedId.value) ?? null)

const labels = ref<JsonObject[]>([])
const labelsLoading = ref(false)
const labelsError = ref<string | null>(null)

const labelContent = ref('')
const expectedPreviousVersion = ref('')
const approveVersions = ref<Record<string, string>>({})
const reviewReasons = ref<Record<string, string>>({})

async function reload(): Promise<void> {
  listLoading.value = true
  listError.value = null
  try {
    const page = await dataLoopApi.listFeedbackCases({
      ...(statusFilter.value ? { status: [statusFilter.value] } : {}),
      limit: 50,
    })
    cases.value = page.cases
    if (selectedId.value && !page.cases.some((item) => item.id === selectedId.value)) {
      selectedId.value = null
    }
  } catch (cause) {
    listError.value = describeError(cause)
  } finally {
    listLoading.value = false
  }
}

async function loadLabels(caseId: string): Promise<void> {
  labelsLoading.value = true
  labelsError.value = null
  try {
    const response = await dataLoopApi.listLabels(caseId)
    labels.value = response.labels
  } catch (cause) {
    labelsError.value = describeError(cause)
    labels.value = []
  } finally {
    labelsLoading.value = false
  }
}

function pickCase(item: FeedbackCase): void {
  selectedId.value = item.id
  labels.value = []
  approveVersions.value = {}
  reviewReasons.value = {}
  void loadLabels(item.id)
}

/** 标签结构由后端定义，这里做保守探测，取不到就让运营手填版本号。 */
function labelId(label: JsonObject): string {
  const value = label.id ?? label.label_id
  return typeof value === 'string' ? value : ''
}

function labelVersion(label: JsonObject): number | null {
  const value = label.version ?? label.label_version
  return typeof value === 'number' ? value : null
}

async function submitLabel(): Promise<void> {
  const current = selected.value
  if (!current) return

  const problem = jsonText(labelContent.value, '标签内容')
  if (problem) {
    toast.warn(problem)
    return
  }
  const versionText = expectedPreviousVersion.value.trim()
  if (versionText && Number.isNaN(Number(versionText))) {
    toast.warn('期望的上一版本号必须是数字')
    return
  }

  const content = parseJson<JsonObject>(labelContent.value, {})
  const body: SubmitFeedbackLabelRequest = {
    ...content,
    idempotency_key: newIdempotencyKey('label'),
    ...(versionText ? { expected_previous_version: Number(versionText) } : {}),
  }

  const result = await run(() => dataLoopApi.submitLabel(current.id, body), {
    success: '标签已提交',
    failure: '提交标签失败',
  })
  if (!result) return
  labelContent.value = ''
  expectedPreviousVersion.value = ''
  await loadLabels(current.id)
}

async function approveLabel(label: JsonObject): Promise<void> {
  const current = selected.value
  if (!current) return

  const id = labelId(label)
  if (!id) {
    toast.warn('该标签缺少 id 字段，无法审批')
    return
  }
  const raw = (approveVersions.value[id] ?? '').trim() || String(labelVersion(label) ?? '')
  if (!raw || Number.isNaN(Number(raw))) {
    toast.warn('请填写该标签的版本号后再审批')
    return
  }

  const confirmed = await dialog.ask({
    title: '确认审批该标签？',
    description: `标签 ${id} 审批后即进入可用于数据集冻结的状态。`,
    confirmText: '确认审批',
  })
  if (!confirmed) return

  const result = await run(
    () =>
      dataLoopApi.approveLabel(current.id, id, {
        expected_label_version: Number(raw),
        idempotency_key: newIdempotencyKey('approve'),
      }),
    { success: '标签已审批', failure: '审批标签失败' },
  )
  if (!result) return
  await loadLabels(current.id)
}

/** 独立二审：通过或退回当前标签版本；退回必须给出可执行修改意见。 */
async function reviewLabel(
  label: JsonObject,
  action: ReviewFeedbackLabelRequest['action'],
): Promise<void> {
  const current = selected.value
  if (!current) return

  const id = labelId(label)
  if (!id) {
    toast.warn('该标签缺少 id 字段，无法二审')
    return
  }
  const raw = (approveVersions.value[id] ?? '').trim() || String(labelVersion(label) ?? '')
  if (!raw || Number.isNaN(Number(raw))) {
    toast.warn('请填写该标签的版本号后再二审')
    return
  }

  const reason = (reviewReasons.value[id] ?? '').trim()
  if (action === 'request_changes' && !reason) {
    toast.warn('退回修改必须填写审核意见')
    return
  }

  const confirmed = await dialog.ask(
    action === 'approve'
      ? {
          title: '确认二审通过该标签？',
          description: `标签 ${id} 二审通过后进入可用于数据集冻结的状态。`,
          confirmText: '二审通过',
        }
      : {
          title: '确认退回该标签？',
          description: `标签 ${id} 将退回给标注人修改。审核意见：${reason}`,
          confirmText: '退回修改',
        },
  )
  if (!confirmed) return

  const result = await run(
    () =>
      dataLoopApi.reviewLabel(current.id, id, {
        action,
        expected_label_version: Number(raw),
        reason: reason || '二审通过',
        idempotency_key: newIdempotencyKey('review'),
      }),
    {
      success: action === 'approve' ? '标签已二审通过' : '标签已退回修改',
      failure: '二审操作失败',
    },
  )
  if (!result) return
  await loadLabels(current.id)
}

onMounted(reload)
</script>

<template>
  <div class="na-grid na-grid--detail">
    <section class="na-card">
      <div class="na-card__head">
        <h2>反馈案例</h2>
        <button class="na-btn" type="button" :disabled="listLoading" @click="reload">
          {{ listLoading ? '加载中…' : '刷新' }}
        </button>
      </div>

      <div class="na-row">
        <div class="na-field na-field--grow">
          <label for="case-status">状态筛选</label>
          <select id="case-status" v-model="statusFilter" class="na-select" @change="reload">
            <option value="">全部状态</option>
            <option v-for="value in STATUS_OPTIONS" :key="value" :value="value">
              {{ feedbackStatusLabel(value) }}
            </option>
          </select>
        </div>
      </div>

      <NaStateBlock
        :loading="listLoading && cases.length === 0"
        :error="listError"
        :empty="cases.length === 0"
        empty-text="暂无反馈案例"
        loading-text="加载反馈案例…"
      >
        <table class="na-table">
          <thead>
            <tr>
              <th>news_id</th>
              <th style="width: 108px">问题类型</th>
              <th style="width: 76px">严重度</th>
              <th style="width: 88px">状态</th>
              <th style="width: 138px">记录时间</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in cases"
              :key="item.id"
              :class="{ 'is-selected': item.id === selectedId }"
              @click="pickCase(item)"
            >
              <td>
                <div class="na-mono">{{ item.news_id }}</div>
                <div class="na-cell__sub">{{ feedbackSourceLabel(item.source_type) }}</div>
              </td>
              <td>{{ problemTypeLabel(item.problem_type) }}</td>
              <td>
                <span class="na-badge" :class="`na-badge--${severityTone(item.severity)}`">
                  {{ severityLabel(item.severity) }}
                </span>
              </td>
              <td>
                <span class="na-badge" :class="`na-badge--${feedbackStatusTone(item.status)}`">
                  {{ feedbackStatusLabel(item.status) }}
                </span>
              </td>
              <td class="na-muted">{{ formatDateTime(item.recorded_at) }}</td>
            </tr>
          </tbody>
        </table>
      </NaStateBlock>
    </section>

    <section class="na-card">
      <div class="na-card__head">
        <h2>案例详情与标注</h2>
        <span v-if="selected" class="na-badge">{{ selected.production_bundle_version }}</span>
      </div>

      <NaStateBlock :empty="!selected" empty-text="从左侧选择一个反馈案例">
        <template v-if="selected">
          <dl class="na-defs">
            <div>
              <dt>案例 ID</dt>
              <dd class="na-mono">{{ selected.id }}</dd>
            </div>
            <div>
              <dt>run_id</dt>
              <dd class="na-mono">{{ selected.run_id ?? '-' }}</dd>
            </div>
            <div>
              <dt>news_id</dt>
              <dd class="na-mono">{{ selected.news_id }}</dd>
            </div>
            <div>
              <dt>来源</dt>
              <dd>{{ feedbackSourceLabel(selected.source_type) }}</dd>
            </div>
            <div>
              <dt>问题类型</dt>
              <dd>{{ problemTypeLabel(selected.problem_type) }}</dd>
            </div>
            <div>
              <dt>发生时间</dt>
              <dd>{{ formatDateTime(selected.occurred_at) }}</dd>
            </div>
            <div>
              <dt>内容摘要</dt>
              <dd class="na-mono">{{ selected.content_sha256 }}</dd>
            </div>
          </dl>

          <NaJsonBlock :value="selected.analysis_input_snapshot" label="分析输入快照" />
          <NaJsonBlock :value="selected.analysis_output_snapshot" label="分析输出快照" />
          <NaJsonBlock :value="selected.source_reference" label="来源引用" />

          <h3>已有标签</h3>
          <NaStateBlock
            :loading="labelsLoading"
            :error="labelsError"
            :empty="labels.length === 0"
            empty-text="该案例还没有标签"
          >
            <table class="na-table">
              <thead>
                <tr>
                  <th>标签 ID</th>
                  <th style="width: 84px">版本</th>
                  <th style="width: 320px">审批 / 二审</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(label, index) in labels" :key="labelId(label) || `label-${index}`">
                  <td class="na-mono">{{ labelId(label) || '(缺少 id)' }}</td>
                  <td>{{ labelVersion(label) ?? '-' }}</td>
                  <td>
                    <div class="na-row">
                      <input
                        v-model="approveVersions[labelId(label)]"
                        class="na-input na-input--narrow"
                        :placeholder="String(labelVersion(label) ?? '版本')"
                        inputmode="numeric"
                      >
                      <input
                        v-model="reviewReasons[labelId(label)]"
                        class="na-input na-field--grow"
                        placeholder="审核意见（退回必填）"
                        maxlength="1000"
                      >
                    </div>
                    <div class="na-row">
                      <button
                        class="na-btn"
                        type="button"
                        :disabled="pending"
                        @click="approveLabel(label)"
                      >
                        审批
                      </button>
                      <button
                        class="na-btn"
                        type="button"
                        :disabled="pending"
                        @click="reviewLabel(label, 'approve')"
                      >
                        二审通过
                      </button>
                      <button
                        class="na-btn na-btn--danger"
                        type="button"
                        :disabled="pending"
                        @click="reviewLabel(label, 'request_changes')"
                      >
                        退回修改
                      </button>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </NaStateBlock>

          <h3>新增标签</h3>
          <div class="na-field">
            <label for="label-content">标签内容（JSON 对象，字段由后端定义）</label>
            <textarea
              id="label-content"
              v-model="labelContent"
              class="na-textarea"
              placeholder="例如：{&quot;label_type&quot;: &quot;evidence_missing&quot;, &quot;notes&quot;: &quot;缺少二级来源&quot;}"
            />
          </div>
          <div class="na-row">
            <div class="na-field na-field--fixed">
              <label for="label-version">期望上一版本（可选）</label>
              <input
                id="label-version"
                v-model="expectedPreviousVersion"
                class="na-input"
                inputmode="numeric"
                placeholder="乐观锁版本号"
              >
            </div>
            <div class="na-field na-field--action">
              <button class="na-btn na-btn--primary" type="button" :disabled="pending" @click="submitLabel">
                提交标签
              </button>
            </div>
          </div>
        </template>
      </NaStateBlock>
    </section>
  </div>
</template>
