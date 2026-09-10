<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { jobsApi } from '@/api'
import type { DecisionAction, DecisionGate, JobStatus } from '@/api'
import EventStreamViewer from '@/components/jobs/EventStreamViewer.vue'
import NaJsonBlock from '@/components/NaJsonBlock.vue'
import { useAsyncTask } from '@/composables/useAsyncTask'
import { useConfirmStore } from '@/stores/confirm'
import { useJobsStore } from '@/stores/jobs'
import type { JobInspectorTab } from '@/stores/jobs'
import { useToastStore } from '@/stores/toast'
import { downloadBlob, timestampedFilename } from '@/utils/download'
import { formatDateTime, formatPercent, truncate } from '@/utils/format'
import { jobStatusLabel, jobStatusTone, stepLabel } from '@/utils/labels'
import { requiredText } from '@/utils/validate'

const props = defineProps<{ jobId: string | null }>()

const store = useJobsStore()
const toast = useToastStore()
const dialog = useConfirmStore()
const { pending, run } = useAsyncTask()

const TABS: Array<{ value: JobInspectorTab; label: string }> = [
  { value: 'overview', label: '概览' },
  { value: 'progress', label: '进度' },
  { value: 'research', label: '研究指标' },
  { value: 'package', label: '资料包' },
  { value: 'recovery', label: '恢复' },
  { value: 'events', label: '事件流' },
]

const job = computed(() => store.selected)

/* ---------------- 切换标签页时按需拉取 ---------------- */

watch(
  [() => props.jobId, () => store.activeTab],
  ([jobId, tab]) => {
    if (!jobId) return
    void store.ensureTabData(jobId, tab)
  },
  { immediate: true },
)

/* ---------------- 人工决策 ---------------- */

const GATES: Array<{ value: DecisionGate; label: string }> = [
  { value: 'research', label: '资料门禁' },
  { value: 'outline', label: '大纲门禁' },
  { value: 'review', label: '评审门禁' },
  { value: 'final', label: '终审门禁' },
]

const ACTIONS: Array<{ value: DecisionAction; label: string }> = [
  { value: 'approve', label: '通过' },
  { value: 'revise', label: '打回修改' },
  { value: 'research', label: '补充检索' },
  { value: 'cancel', label: '取消任务' },
]

const gate = ref<DecisionGate>('research')
const action = ref<DecisionAction>('approve')
const instruction = ref('')

/** 按当前状态推断最可能的门禁，减少运营选错的机会。 */
function suggestGate(status: JobStatus): DecisionGate {
  switch (status) {
    case 'research_review':
      return 'research'
    case 'outline_review':
      return 'outline'
    case 'reviewing':
    case 'revising':
      return 'review'
    default:
      return 'final'
  }
}

watch(
  () => job.value?.status,
  (status) => {
    if (!status) return
    gate.value = suggestGate(status)
    action.value = 'approve'
  },
  { immediate: true },
)

const needsInstruction = computed(() => action.value === 'revise' || action.value === 'research')

async function submitDecision(): Promise<void> {
  const current = job.value
  if (!current) return

  const problem = needsInstruction.value
    ? requiredText(instruction.value, '打回修改或补充检索必须填写指令说明')
    : null
  if (problem) {
    toast.warn(problem)
    return
  }

  if (action.value === 'cancel') {
    const confirmed = await dialog.ask({
      title: '确认取消该任务？',
      description: `${current.topic}（${current.id}）取消后任务不会继续执行。`,
      confirmText: '确认取消',
      danger: true,
    })
    if (!confirmed) return
  }

  const result = await run(
    () =>
      store.decide(current.id, {
        gate: gate.value,
        action: action.value,
        instruction: instruction.value.trim() || null,
      }),
    { success: '决策已受理', failure: '提交决策失败' },
  )
  if (result) instruction.value = ''
}

/* ---------------- 发布与导出 ---------------- */

const channel = ref('default')

async function publish(): Promise<void> {
  const current = job.value
  if (!current) return

  const target = channel.value.trim() || 'default'
  const confirmed = await dialog.ask({
    title: '确认发布到 CMS？',
    description: `任务 ${current.id} 将推送到渠道「${target}」。发布是对外动作，请确认终稿已审核。`,
    confirmText: '确认发布',
  })
  if (!confirmed) return

  const result = await run(() => store.publish(current.id, { channel: target }), {
    success: '发布成功',
    failure: '发布失败',
  })
  if (result) {
    toast.info(`外部发布 ID：${result.external_publication_id}`)
  }
}

async function exportFinal(): Promise<void> {
  const current = job.value
  if (!current) return
  const blob = await run(() => jobsApi.exportFinal(current.id), { failure: '导出成稿失败' })
  if (!blob) return
  downloadBlob(blob, timestampedFilename(`job-${current.id.slice(0, 8)}-final`, 'blob'))
  toast.ok('成稿已开始下载')
}

async function exportResearchPackage(): Promise<void> {
  const current = job.value
  if (!current) return
  const blob = await run(() => jobsApi.exportResearchPackage(current.id), {
    failure: '导出资料包失败',
  })
  if (!blob) return
  downloadBlob(blob, timestampedFilename(`job-${current.id.slice(0, 8)}-research`, 'blob'))
  toast.ok('资料包已开始下载')
}

/* ---------------- 断点恢复 ---------------- */

const resumeToken = ref('')
const recoveryAction = ref<'' | 'research'>('')
const resumeInstruction = ref('')

/** 恢复点结构由后端定义，这里只做保守探测，取不到就让运营手填。 */
function pickResumeToken(point: unknown): string {
  if (!point || typeof point !== 'object') return ''
  const record = point as Record<string, unknown>
  for (const key of ['resume_token', 'token', 'checkpoint_token']) {
    const value = record[key]
    if (typeof value === 'string' && value) return value
  }
  return ''
}

watch(
  () => store.recovery,
  (plan) => {
    resumeToken.value = plan ? pickResumeToken(plan.resume_point) : ''
    recoveryAction.value = plan?.requires_human ? 'research' : ''
  },
  { immediate: true },
)

async function submitResume(): Promise<void> {
  const current = job.value
  const plan = store.recovery
  if (!current || !plan) return

  const problem = requiredText(resumeToken.value, '恢复令牌')
  if (problem) {
    toast.warn(problem)
    return
  }

  const confirmed = await dialog.ask({
    title: '确认执行断点恢复？',
    description: plan.reason || '将从最近一次检查点继续执行该任务。',
    confirmText: '确认恢复',
  })
  if (!confirmed) return

  const result = await run(
    () =>
      store.resume(current.id, {
        resume_token: resumeToken.value.trim(),
        recovery_action: recoveryAction.value || null,
        instruction: resumeInstruction.value.trim() || null,
      }),
    { success: '恢复请求已受理', failure: '恢复失败' },
  )
  if (result) resumeInstruction.value = ''
}

/* ---------------- 展示辅助 ---------------- */

const isStreaming = computed(() => store.streamState === 'open' || store.streamState === 'connecting')
</script>

<template>
  <section class="na-card">
    <div class="na-card__head">
      <h2>任务详情</h2>
      <div class="na-row">
        <span v-if="job" class="na-badge" :class="`na-badge--${jobStatusTone(job.status)}`">
          {{ jobStatusLabel(job.status) }}
        </span>
        <span class="na-badge">{{ store.streamState }}</span>
        <button
          class="na-btn"
          type="button"
          :disabled="!jobId || store.detailLoading"
          @click="jobId && store.fetchDetail(jobId)"
        >
          刷新
        </button>
      </div>
    </div>

    <NaStateBlock
      :loading="store.detailLoading && !job"
      :error="store.detailError"
      :empty="!jobId"
      empty-text="从左侧列表选择一个任务"
      loading-text="加载任务详情…"
    >
      <template v-if="job">
        <div class="na-metrics">
          <div class="na-metric">
            <div class="na-metric__label">当前步骤</div>
            <div class="na-metric__value">{{ stepLabel(job.current_step) }}</div>
          </div>
          <div class="na-metric">
            <div class="na-metric__label">整体进度</div>
            <div class="na-metric__value">{{ formatPercent(job.progress_percent) }}</div>
          </div>
          <div class="na-metric">
            <div class="na-metric__label">章节进度</div>
            <div class="na-metric__value">
              {{ job.sections_completed }} / {{ job.sections_total }}
            </div>
          </div>
          <div class="na-metric">
            <div class="na-metric__label">评审轮次</div>
            <div class="na-metric__value">{{ job.review_rounds }}</div>
          </div>
          <div class="na-metric">
            <div class="na-metric__label">检索重试</div>
            <div class="na-metric__value">{{ job.research_retries }}</div>
          </div>
        </div>

        <div class="na-tabs" role="tablist">
          <button
            v-for="tab in TABS"
            :key="tab.value"
            class="na-tab"
            :class="{ 'is-active': store.activeTab === tab.value }"
            type="button"
            role="tab"
            :aria-selected="store.activeTab === tab.value"
            @click="store.activeTab = tab.value"
          >
            {{ tab.label }}
          </button>
        </div>

        <div v-if="store.activeTab === 'overview'" class="na-panel">
          <dl class="na-defs">
            <div>
              <dt>选题</dt>
              <dd>{{ job.topic }}</dd>
            </div>
            <div>
              <dt>任务 ID</dt>
              <dd class="na-mono">{{ job.id }}</dd>
            </div>
            <div>
              <dt>租户</dt>
              <dd class="na-mono">{{ job.tenant_id }}</dd>
            </div>
            <div>
              <dt>产出类型</dt>
              <dd>{{ job.scenario === 'assisted_writing' ? '辅助写作' : '仅资料包' }}</dd>
            </div>
            <div>
              <dt>Temporal 工作流</dt>
              <dd class="na-mono">{{ job.temporal_workflow_id }}</dd>
            </div>
            <div>
              <dt>创建时间</dt>
              <dd>{{ formatDateTime(job.created_at) }}</dd>
            </div>
            <div>
              <dt>更新时间</dt>
              <dd>{{ formatDateTime(job.updated_at) }}</dd>
            </div>
          </dl>
          <NaJsonBlock :value="job.requirements" label="requirements" />
        </div>

        <div v-else-if="store.activeTab === 'progress'" class="na-panel">
          <NaStateBlock
            :loading="store.progressLoading"
            :error="store.progressError"
            :empty="!store.progress"
            empty-text="暂无进度快照"
          >
            <template v-if="store.progress">
              <dl class="na-defs">
                <div>
                  <dt>工作流阶段</dt>
                  <dd>{{ store.progress.workflow?.phase ?? '-' }}</dd>
                </div>
                <div>
                  <dt>已完成步骤</dt>
                  <dd>{{ store.progress.workflow?.completed_steps ?? '-' }}</dd>
                </div>
                <div>
                  <dt>评审轮次</dt>
                  <dd>{{ store.progress.workflow?.review_round ?? '-' }}</dd>
                </div>
                <div>
                  <dt>等待门禁</dt>
                  <dd>{{ store.progress.workflow?.waiting_gate ?? '-' }}</dd>
                </div>
                <div>
                  <dt>最近产物</dt>
                  <dd class="na-mono">{{ truncate(store.progress.workflow?.last_artifact_uri ?? '-', 64) }}</dd>
                </div>
              </dl>
              <NaJsonBlock :value="store.progress" label="进度快照原始载荷" />
            </template>
          </NaStateBlock>
        </div>

        <div v-else-if="store.activeTab === 'research'" class="na-panel">
          <NaStateBlock
            :loading="store.metricsLoading"
            :error="store.metricsError"
            :empty="!store.metrics"
            empty-text="暂无研究指标"
          >
            <template v-if="store.metrics">
              <div class="na-metrics">
                <div class="na-metric">
                  <div class="na-metric__label">逻辑键</div>
                  <div class="na-metric__value na-mono">{{ store.metrics.logical_key }}</div>
                </div>
                <div class="na-metric">
                  <div class="na-metric__label">产物版本</div>
                  <div class="na-metric__value">v{{ store.metrics.artifact_version }}</div>
                </div>
              </div>
              <NaJsonBlock :value="store.metrics.metrics" label="研究指标" :collapsed-height="320" />
            </template>
          </NaStateBlock>
        </div>

        <div v-else-if="store.activeTab === 'package'" class="na-panel">
          <NaStateBlock
            :loading="store.packageLoading"
            :error="store.packageError"
            :empty="!store.researchPackage"
            empty-text="资料包尚未生成"
          >
            <div class="na-row na-row--end">
              <button class="na-btn" type="button" :disabled="pending" @click="exportResearchPackage">
                导出资料包
              </button>
            </div>
            <NaJsonBlock :value="store.researchPackage" label="资料包" :collapsed-height="380" />
          </NaStateBlock>
        </div>

        <div v-else-if="store.activeTab === 'recovery'" class="na-panel">
          <NaStateBlock
            :loading="store.recoveryLoading"
            :error="store.recoveryError"
            :empty="!store.recovery"
            empty-text="暂无恢复计划"
          >
            <template v-if="store.recovery">
              <div class="na-metrics">
                <div class="na-metric">
                  <div class="na-metric__label">可恢复</div>
                  <div class="na-metric__value">{{ store.recovery.can_resume ? '是' : '否' }}</div>
                </div>
                <div class="na-metric">
                  <div class="na-metric__label">需要人工</div>
                  <div class="na-metric__value">{{ store.recovery.requires_human ? '是' : '否' }}</div>
                </div>
              </div>
              <p class="na-muted">{{ store.recovery.reason }}</p>

              <h3>执行恢复</h3>
              <div class="na-row">
                <div class="na-field na-field--grow">
                  <label for="resume-token">恢复令牌</label>
                  <input
                    id="resume-token"
                    v-model="resumeToken"
                    class="na-input"
                    placeholder="来自恢复计划的 resume_point"
                    autocomplete="off"
                  >
                </div>
                <div class="na-field na-field--fixed">
                  <label for="resume-action">恢复动作</label>
                  <select id="resume-action" v-model="recoveryAction" class="na-select">
                    <option value="">按检查点继续</option>
                    <option value="research">重新检索</option>
                  </select>
                </div>
                <div class="na-field na-field--action">
                  <button
                    class="na-btn na-btn--primary"
                    type="button"
                    :disabled="pending || !store.recovery.can_resume"
                    @click="submitResume"
                  >
                    执行恢复
                  </button>
                </div>
              </div>
              <div class="na-field">
                <label for="resume-instruction">补充指令（可选）</label>
                <input id="resume-instruction" v-model="resumeInstruction" class="na-input">
              </div>

              <NaJsonBlock :value="store.recovery.resume_point" label="恢复点" />
            </template>
          </NaStateBlock>
        </div>

        <div v-else class="na-panel">
          <EventStreamViewer
            :events="store.events"
            :stream-state="store.streamState"
            :stream-error="store.streamError"
          />
          <div class="na-row na-row--end">
            <button
              class="na-btn"
              type="button"
              :disabled="isStreaming"
              @click="jobId && store.connectStream(jobId)"
            >
              重新连接事件流
            </button>
          </div>
        </div>

        <h3>人工决策</h3>
        <div class="na-row">
          <div class="na-field na-field--fixed">
            <label for="gate">门禁</label>
            <select id="gate" v-model="gate" class="na-select">
              <option v-for="item in GATES" :key="item.value" :value="item.value">
                {{ item.label }}
              </option>
            </select>
          </div>
          <div class="na-field na-field--fixed">
            <label for="action">动作</label>
            <select id="action" v-model="action" class="na-select">
              <option v-for="item in ACTIONS" :key="item.value" :value="item.value">
                {{ item.label }}
              </option>
            </select>
          </div>
          <div class="na-field na-field--action">
            <button class="na-btn na-btn--primary" type="button" :disabled="pending" @click="submitDecision">
              提交决策
            </button>
          </div>
        </div>
        <div class="na-field">
          <label for="instruction">
            指令
            <span v-if="needsInstruction" class="na-required">（打回修改 / 补充检索必填）</span>
          </label>
          <textarea
            id="instruction"
            v-model="instruction"
            class="na-textarea"
            placeholder="说明需要修改什么、或补充哪些方向的检索"
          />
        </div>

        <h3>发布与导出</h3>
        <div class="na-row">
          <div class="na-field na-field--fixed">
            <label for="channel">发布渠道</label>
            <input id="channel" v-model="channel" class="na-input" autocomplete="off">
          </div>
          <div class="na-field na-field--action">
            <button class="na-btn" type="button" :disabled="pending" @click="publish">发布到 CMS</button>
          </div>
          <div class="na-field na-field--action">
            <button class="na-btn" type="button" :disabled="pending" @click="exportFinal">导出成稿</button>
          </div>
        </div>
      </template>
    </NaStateBlock>
  </section>
</template>
