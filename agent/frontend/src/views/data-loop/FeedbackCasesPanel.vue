<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

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
  operationsFeedbackCategoryLabel,
  operationsReasonLabel,
  problemTypeLabel,
  severityLabel,
  severityTone,
} from '@/utils/labels'
import { readOperationsFeedback } from '@/utils/operationsFeedback'
import { jsonText, parseJson } from '@/utils/validate'
import { writingJobId } from '@/utils/capabilityReceipts'

const toast = useToastStore()
const route = useRoute()
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
const PAGE_SIZE = 50
const pageOffset = ref(0)
const hasNextPage = ref(false)

const selectedId = ref<string | null>(null)
const linkedCase = ref<FeedbackCase | null>(null)
const linkedLoading = ref(false)
const linkedError = ref<string | null>(null)
let linkedRequest = 0
let disposed = false
const selected = computed(() => linkedCase.value?.id === selectedId.value ? linkedCase.value
  : cases.value.find((item) => item.id === selectedId.value) ?? null)
const selectedFeedback = computed(() =>
  selected.value ? readOperationsFeedback(selected.value.source_reference) : null,
)

function caseClassification(item: FeedbackCase): string {
  const feedback = readOperationsFeedback(item.source_reference)
  return feedback
    ? operationsFeedbackCategoryLabel(feedback.category)
    : problemTypeLabel(item.problem_type)
}

const labels = ref<JsonObject[]>([])
const labelsLoading = ref(false)
const labelsError = ref<string | null>(null)

const labelContent = ref('')
const expectedPreviousVersion = ref('')
const approveVersions = ref<Record<string, string>>({})
const reviewReasons = ref<Record<string, string>>({})

async function reload(offset = pageOffset.value): Promise<void> {
  listLoading.value = true
  listError.value = null
  try {
    const page = await dataLoopApi.listFeedbackCases({
      ...(statusFilter.value ? { statuses: [statusFilter.value] } : {}),
      offset,
      limit: PAGE_SIZE + 1,
    })
    cases.value = page.cases.slice(0, PAGE_SIZE)
    pageOffset.value = offset
    hasNextPage.value = page.cases.length > PAGE_SIZE
    if (selectedId.value && linkedCase.value?.id !== selectedId.value && !cases.value.some((item) => item.id === selectedId.value)) {
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
    if (disposed || selectedId.value !== caseId) return
    labels.value = response.labels
  } catch (cause) {
    if (disposed || selectedId.value !== caseId) return
    labelsError.value = describeError(cause)
    labels.value = []
  } finally {
    if (!disposed && selectedId.value === caseId) labelsLoading.value = false
  }
}

function pickCase(item: FeedbackCase): void {
  selectedId.value = item.id
  labels.value = []
  approveVersions.value = {}
  reviewReasons.value = {}
  void loadLabels(item.id)
}

watch(() => route.query.case_id, async (value) => {
  const sequence = ++linkedRequest
  linkedCase.value = null
  linkedError.value = null
  linkedLoading.value = false
  labels.value = []
  labelsLoading.value = false
  labelsError.value = null
  if (value === undefined) return
  const id = writingJobId(value)
  selectedId.value = null
  if (!id) { linkedError.value = '链接中的案例 ID 不合法，请从列表选择。'; return }
  linkedLoading.value = true
  try {
    const response = await dataLoopApi.getFeedbackCase(id)
    if (disposed || sequence !== linkedRequest) return
    if (response.case?.id !== id) throw new Error('反馈案例响应与链接目标不一致')
    linkedCase.value = response.case
    pickCase(response.case)
  } catch (cause) {
    if (!disposed && sequence === linkedRequest) linkedError.value = describeError(cause)
  } finally {
    if (!disposed && sequence === linkedRequest) linkedLoading.value = false
  }
}, { immediate: true })
onBeforeUnmount(() => { disposed = true; ++linkedRequest })

/** 标签结构由后端定义，这里做保守探测，取不到就让运营手填版本号。 */
function labelId(label: JsonObject): string {
  const value = label.id ?? label.label_id
  return typeof value === 'string' ? value : ''
}

function labelVerdict(label: JsonObject): string {
  const labels: Record<string, string> = { correct: '分析正确', incorrect: '分析有误', partially_correct: '部分正确', not_evaluable: '无法评测' }
  return typeof label.verdict === 'string' ? labels[label.verdict] ?? label.verdict : '待核对'
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

onMounted(() => reload())
</script>

<template>
  <div class="na-grid na-grid--detail feedback-workspace">
    <section class="na-card">
      <div class="na-card__head">
        <h2>反馈案例</h2>
        <button class="na-btn" type="button" :disabled="listLoading" @click="reload()">
          {{ listLoading ? '加载中…' : '刷新' }}
        </button>
      </div>

      <div class="na-row">
        <div class="na-field na-field--grow">
          <label for="case-status">状态筛选</label>
          <select id="case-status" v-model="statusFilter" class="na-select" :disabled="listLoading" @change="reload(0)">
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
        <div class="na-table-wrap" tabindex="0" aria-label="反馈案例列表，可横向滚动">
          <table class="na-table feedback-cases-table">
            <thead>
              <tr>
                <th>关联新闻</th>
                <th style="width: 108px">反馈分类 / 问题</th>
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
                <td>{{ caseClassification(item) }}</td>
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
        </div>
      </NaStateBlock>

      <div class="na-pager">
        <span class="na-muted">第 {{ Math.floor(pageOffset / PAGE_SIZE) + 1 }} 页 · 本页 {{ cases.length }} 条</span>
        <div class="na-pager__actions">
          <button class="na-btn" type="button" :disabled="listLoading || pageOffset === 0" @click="reload(0)">首页</button>
          <button class="na-btn" type="button" :disabled="listLoading || pageOffset === 0" @click="reload(Math.max(0, pageOffset - PAGE_SIZE))">上一页</button>
          <button class="na-btn" type="button" :disabled="listLoading || !hasNextPage" @click="reload(pageOffset + PAGE_SIZE)">下一页</button>
        </div>
      </div>
    </section>

    <section class="na-card">
      <div class="na-card__head">
        <h2>案例详情与标注</h2>
        <span v-if="selected" class="na-badge" :class="`na-badge--${feedbackStatusTone(selected.status)}`">{{ feedbackStatusLabel(selected.status) }}</span>
      </div>

      <NaStateBlock :loading="linkedLoading" :error="linkedError" :empty="!selected" empty-text="从左侧选择一个反馈案例">
        <template v-if="selected">
          <dl class="na-defs">
            <div><dt>关联新闻</dt><dd>{{ selected.news_id }}</dd></div>
            <div><dt>问题类型</dt><dd>{{ problemTypeLabel(selected.problem_type) }}</dd></div>
            <template v-if="selectedFeedback">
              <div><dt>反馈分类</dt><dd>{{ operationsFeedbackCategoryLabel(selectedFeedback.category) }}</dd></div>
              <div><dt>反馈理由</dt><dd>{{ selectedFeedback.reason_codes.map(operationsReasonLabel).join('、') }}</dd></div>
            </template>
            <div><dt>发生时间</dt><dd>{{ formatDateTime(selected.occurred_at) }}</dd></div>
          </dl>
          <p v-if="selectedFeedback" class="na-notice">反馈尚需独立标注和另一人二审，才可用于评测。</p>
          <details class="na-disclosure">
            <summary>查看原始证据与来源</summary>
            <dl class="na-defs">
              <div><dt>案例编号</dt><dd class="na-mono">{{ selected.id }}</dd></div>
              <div><dt>来源运行</dt><dd class="na-mono">{{ selected.run_id ?? '-' }}</dd></div>
              <div><dt>来源类型</dt><dd>{{ feedbackSourceLabel(selected.source_type) }}</dd></div>
              <div><dt>配置版本</dt><dd>{{ selected.production_bundle_version }}</dd></div>
              <div><dt>内容校验值</dt><dd class="na-mono">{{ selected.content_sha256 }}</dd></div>
            </dl>
            <NaJsonBlock :value="selected.analysis_input_snapshot" label="分析输入快照" />
            <NaJsonBlock :value="selected.analysis_output_snapshot" label="分析输出快照" />
            <NaJsonBlock :value="selected.source_reference" label="来源引用" />
          </details>

          <h3>已有标签</h3>
          <NaStateBlock
            :loading="labelsLoading"
            :error="labelsError"
            :empty="labels.length === 0"
            empty-text="该案例还没有标签"
          >
            <div class="na-table-wrap" tabindex="0" aria-label="标签审批与二审列表，可横向滚动">
              <table class="na-table feedback-labels-table">
                <thead>
                  <tr>
                    <th>标签内容</th>
                    <th style="width: 84px">版本</th>
                    <th style="width: 320px">审批 / 二审</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(label, index) in labels" :key="labelId(label) || `label-${index}`">
                    <td>
                      <div>{{ labelVerdict(label) }}</div>
                      <p v-if="label.operator_comment">{{ label.operator_comment }}</p>
                      <details class="na-disclosure"><summary>完整标签与证据要求</summary><NaJsonBlock :value="label" label="完整标签" /></details>
                    </td>
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
            </div>
          </NaStateBlock>

          <details class="na-disclosure">
            <summary>新增标签</summary>
            <div class="na-field">
              <label for="label-content">完整标签（JSON）</label>
              <textarea
                id="label-content"
                v-model="labelContent"
                class="na-textarea"
                placeholder="例如：{&quot;verdict&quot;: &quot;not_evaluable&quot;, &quot;operator_comment&quot;: &quot;证据不足，暂不参与评测&quot;}"
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
                  placeholder="已有标签的版本号"
                >
              </div>
              <div class="na-field na-field--action">
                <button class="na-btn na-btn--primary" type="button" :disabled="pending" @click="submitLabel">
                  提交标签
                </button>
              </div>
            </div>
          </details>
        </template>
      </NaStateBlock>
    </section>
  </div>
</template>

<style scoped>
.feedback-workspace > .na-card {
  min-width: 0;
}

.feedback-cases-table {
  min-width: 700px;
}

.feedback-cases-table th:first-child,
.feedback-cases-table td:first-child {
  min-width: 200px;
}

.feedback-labels-table {
  min-width: 760px;
}

.feedback-labels-table th:first-child,
.feedback-labels-table td:first-child {
  min-width: 260px;
}
</style>
