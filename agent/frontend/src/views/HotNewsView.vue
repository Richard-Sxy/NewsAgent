<script setup lang="ts">
import { nextTick, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { hotNewsApi } from '@/api'
import type { HotNewsRankedItemView, HotNewsRunDetailResponse, HotNewsRunSummary } from '@/api'
import NaStateBlock from '@/components/NaStateBlock.vue'
import HotNewsDecisionForm from '@/components/hotnews/HotNewsDecisionForm.vue'
import HotNewsRankTable from '@/components/hotnews/HotNewsRankTable.vue'
import HotNewsQueryTools from '@/components/hotnews/HotNewsQueryTools.vue'
import HotNewsSqlToolTrace from '@/components/hotnews/HotNewsSqlToolTrace.vue'
import type { SqlAssistantPreviewRequest } from '@/api/sqlAssistant'
import { useAppStore } from '@/stores/app'
import { describeError } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'
import { decisionTypeLabel } from '@/utils/labels'

const appStore = useAppStore()
const route = useRoute()
const localSimulationEnabled = import.meta.env.DEV && import.meta.env.VITE_LOCAL_SIMULATION === '1'
const simulationRunning = ref(false)
const simulationMessage = ref<string | null>(null)
const simulationError = ref<string | null>(null)

const runs = ref<HotNewsRunSummary[]>([])
const runsLoading = ref(false)
const runsError = ref<string | null>(null)

const selectedRunId = ref<string | null>(null)
const detail = ref<HotNewsRunDetailResponse | null>(null)
const detailLoading = ref(false)
const detailError = ref<string | null>(null)
let detailRequestSequence = 0

/** 决策表单预填的 news_id；点榜单行的"决策"按钮时更新。 */
const decisionNewsId = ref('')
const decisionFormOpen = ref(false)
const decisionForm = ref<HTMLDetailsElement | null>(null)

async function loadRuns(): Promise<void> {
  runsLoading.value = true
  runsError.value = null
  try {
    const page = await hotNewsApi.listRuns({ limit: 20 })
    runs.value = page.runs
    if (page.runs.length > 0 && !selectedRunId.value) {
      await selectRun(page.runs[0].run_id)
    }
  } catch (cause) {
    runsError.value = describeError(cause)
  } finally {
    runsLoading.value = false
  }
}

async function selectRun(runId: string): Promise<void> {
  const sequence = ++detailRequestSequence
  selectedRunId.value = runId
  detailLoading.value = true
  detailError.value = null
  detail.value = null
  try {
    const value = await hotNewsApi.runDetail(runId)
    if (sequence === detailRequestSequence) detail.value = value
  } catch (cause) {
    if (sequence === detailRequestSequence) detailError.value = describeError(cause)
  } finally {
    if (sequence === detailRequestSequence) detailLoading.value = false
  }
}

async function runLocalSimulation(request: SqlAssistantPreviewRequest): Promise<void> {
  if (simulationRunning.value) return
  simulationRunning.value = true
  simulationMessage.value = null
  simulationError.value = null
  try {
    const result = await hotNewsApi.runLocalSimulation(request)
    selectedRunId.value = result.run_id
    await loadRuns()
    await selectRun(result.run_id)
    simulationMessage.value = `热点分析完成：上榜 ${result.ranked_news_count} 篇，已分析 ${result.analyzed_news_count} 篇。`
  } catch (cause) {
    simulationError.value = describeError(cause)
  } finally {
    simulationRunning.value = false
  }
}

function prefillDecision(item: HotNewsRankedItemView): void {
  decisionNewsId.value = item.news_id
  decisionFormOpen.value = true
  void nextTick(() => decisionForm.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
}

function syncDecisionForm(event: Event): void {
  decisionFormOpen.value = (event.target as HTMLDetailsElement).open
}

async function onDecisionRecorded(): Promise<void> {
  if (selectedRunId.value) await selectRun(selectedRunId.value)
}

onMounted(loadRuns)
</script>

<template>
  <header class="na-page-head">
    <div>
      <p class="na-eyebrow">新闻洞察</p>
      <h1>热点工作台</h1>
      <p class="na-subtitle">查看热点榜单与分析，记录运营判断。</p>
    </div>
    <button class="na-btn na-btn--quiet" type="button" :disabled="runsLoading" @click="loadRuns">
      {{ runsLoading ? '刷新中…' : '刷新榜单' }}
    </button>
  </header>

  <p v-if="!appStore.hotNewsEnabled" class="na-notice">当前未开放热点功能。</p>
  <p v-if="detail?.sql_tool_trace?.preview.schema_version === 'news-warehouse-v6'" class="na-notice">
    真实新闻正文 · 模拟指标与基线。栏目为演示分类，新闻原始发布时间与指标窗口独立。
  </p>

  <details
    v-if="localSimulationEnabled"
    id="query-tools"
    class="na-card na-disclosure hotnews-disclosure hotnews-query"
    :open="route.hash === '#query-tools'"
  >
    <summary>
      <span>查询热点新闻</span>
      <span class="hotnews-disclosure__hint">本地测试</span>
    </summary>
    <HotNewsQueryTools id="hotnews-query-form" :running="simulationRunning" @run="runLocalSimulation" />
  </details>

  <p v-if="simulationMessage" class="na-message na-message--ok" role="status">
    {{ simulationMessage }}
  </p>
  <p v-if="simulationError" class="na-message na-message--error" role="alert">
    {{ simulationError }}
  </p>

  <section class="na-card hotnews-board">
    <div class="na-card__head">
      <h2>热点榜单</h2>
      <span v-if="detail" class="na-badge" :class="{ 'na-badge--live': detail.run.status === 'completed' }">
        {{ detail.run.status === 'completed' ? '已完成' : '状态待确认' }}
      </span>
    </div>

    <NaStateBlock
      :loading="runsLoading && runs.length === 0"
      :error="runsError"
      :empty="runs.length === 0"
      empty-text="当前租户还没有已完成的热点运行"
      loading-text="加载热点运行…"
    >
      <div class="hotnews-window">
        <div class="na-field">
          <label for="hotnews-run">分析窗口</label>
          <select
            id="hotnews-run"
            class="na-select"
            :value="selectedRunId ?? ''"
            :disabled="simulationRunning"
            @change="selectRun(($event.target as HTMLSelectElement).value)"
          >
            <option v-for="run in runs" :key="run.run_id" :value="run.run_id">
              {{ formatDateTime(run.window_start) }} ～ {{ formatDateTime(run.window_end) }} · 完成于 {{ formatDateTime(run.completed_at) }}
            </option>
          </select>
        </div>
        <p v-if="detail" class="hotnews-window__summary">
          <span>上榜 <strong>{{ detail.run.ranked_news_count }}</strong> 篇</span>
          <span>已分析 <strong>{{ detail.run.analyzed_news_count }}</strong> 篇</span>
        </p>
      </div>

      <NaStateBlock
        :loading="detailLoading"
        :error="detailError"
        :empty="!detail"
        empty-text="选择一次运行查看榜单"
        loading-text="加载榜单…"
      >
        <template v-if="detail">
          <div class="na-table-wrap hotnews-rank" tabindex="0" aria-label="热点榜单，可横向滚动">
            <HotNewsRankTable :items="detail.ranked_news" :run-id="detail.run.run_id" @decide="prefillDecision" />
          </div>
          <p class="hotnews-board__note">热度由后端确定性计算，分析与证据可在榜单中展开查看。</p>
          <details class="na-disclosure">
            <summary>运行详情</summary>
            <dl class="na-defs">
              <div><dt>运行编号</dt><dd class="na-mono">{{ detail.run.run_id }}</dd></div>
              <div><dt>配置版本</dt><dd>{{ detail.run.production_bundle_version }}</dd></div>
              <div><dt>行为记录</dt><dd>{{ detail.run.fetched_record_count }} 条</dd></div>
              <div><dt>指标快照</dt><dd>{{ detail.run.metric_snapshot_count }} 篇</dd></div>
            </dl>
            <p v-if="detail.sql_tool_trace" class="na-muted">取数证据与当前榜单属于同一次运行。最终热度由后端计算，可能与候选筛选顺序不同。</p>
            <p v-if="detail.analysis_execution" class="na-muted">已分析 {{ detail.analysis_execution.task_count }} 篇 · 耗时 {{ detail.analysis_execution.elapsed_ms }} 毫秒 · 并发峰值 {{ detail.analysis_execution.observed_concurrency }} / 上限 {{ detail.analysis_execution.max_concurrency }}</p>
          </details>
        </template>
      </NaStateBlock>
    </NaStateBlock>
  </section>

  <details v-if="runs.length" class="na-card na-disclosure hotnews-disclosure">
    <summary>
      <span>历史运行</span>
      <span class="hotnews-disclosure__hint">最近 {{ runs.length }} 次</span>
    </summary>
    <div class="na-table-wrap" tabindex="0" aria-label="历史运行，可横向滚动">
      <table class="na-table hotnews-history">
        <thead>
          <tr>
            <th>分析窗口</th>
            <th>上榜 / 已分析</th>
            <th>完成时间</th>
            <th><span class="hotnews-sr-only">操作</span></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="run in runs" :key="run.run_id" :class="{ 'is-selected': run.run_id === selectedRunId }">
            <td>{{ formatDateTime(run.window_start) }} ～ {{ formatDateTime(run.window_end) }}</td>
            <td>{{ run.ranked_news_count }} / {{ run.analyzed_news_count }} 篇</td>
            <td>{{ formatDateTime(run.completed_at) }}</td>
            <td>
              <button class="na-btn na-btn--quiet" type="button" :disabled="simulationRunning" @click="selectRun(run.run_id)">
                {{ run.run_id === selectedRunId ? '当前' : '查看' }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </details>

  <details v-if="detail?.sql_tool_trace" class="na-card na-disclosure hotnews-disclosure hotnews-evidence">
    <summary>查询过程与完整取数证据</summary>
    <HotNewsSqlToolTrace :key="detail.run.run_id" :run-id="detail.run.run_id" :trace="detail.sql_tool_trace" />
  </details>

  <details v-if="detail && detail.decisions.length" class="na-card na-disclosure hotnews-disclosure">
    <summary>
      <span>本次决策记录</span>
      <span class="hotnews-disclosure__hint">{{ detail.decisions.length }} 条</span>
    </summary>
    <div class="na-table-wrap" tabindex="0" aria-label="本次运营决策记录，可横向滚动">
      <table class="na-table hotnews-decisions">
        <thead>
          <tr>
            <th>关联新闻</th>
            <th style="width: 84px">决策</th>
            <th>原因</th>
            <th style="width: 160px">操作者</th>
            <th style="width: 170px">时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in detail.decisions" :key="item.decision_id">
            <td class="na-mono">{{ item.news_id }}</td>
            <td>{{ decisionTypeLabel(item.decision_type) }}</td>
            <td>{{ item.reason }}</td>
            <td class="na-mono">{{ item.operator_id.slice(0, 8) }}…</td>
            <td>{{ formatDateTime(item.created_at) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </details>

  <details
    v-if="selectedRunId"
    ref="decisionForm"
    class="na-card na-disclosure hotnews-disclosure hotnews-decision"
    :open="decisionFormOpen"
    @toggle="syncDecisionForm"
  >
    <summary>
      <span>记录运营决策</span>
      <span class="hotnews-disclosure__hint">采纳、驳回、延后或修正</span>
    </summary>
    <HotNewsDecisionForm
      :run-id="selectedRunId"
      :news-id="decisionNewsId"
      @recorded="onDecisionRecorded"
    />
  </details>
</template>

<style scoped>
.hotnews-disclosure {
  margin-top: 0;
  padding-block: 0;
}

.hotnews-disclosure > summary {
  padding-block: 16px;
  color: var(--na-text);
  font-weight: 500;
}

.hotnews-disclosure[open] {
  padding-bottom: 20px;
}

.hotnews-query,
.hotnews-decision {
  scroll-margin-top: 20px;
}

.hotnews-disclosure__hint {
  margin-left: 12px;
  color: var(--na-text-muted);
  font-size: 12px;
  font-weight: 400;
}

.hotnews-window {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding-bottom: 8px;
}

.hotnews-window .na-field {
  flex: 0 1 520px;
  min-width: 0;
}

.hotnews-window__summary {
  display: flex;
  gap: 16px;
  color: var(--na-text-muted);
  font-size: 12px;
  white-space: nowrap;
}

.hotnews-window__summary strong {
  margin-inline: 4px;
  color: var(--na-text);
  font-size: 16px;
  font-weight: 600;
}

.hotnews-rank :deep(.na-table) {
  min-width: 840px;
}

.hotnews-board__note {
  margin: 12px 0 0;
  color: var(--na-text-muted);
  font-size: 12px;
}

.hotnews-history {
  min-width: 760px;
}

.hotnews-decisions {
  min-width: 700px;
}

.hotnews-query :deep(.na-card),
.hotnews-evidence :deep(.na-card),
.hotnews-decision :deep(.na-card) {
  padding: 0;
  border: none;
  box-shadow: none;
}

.hotnews-sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
}

@media (max-width: 700px) {
  .hotnews-window {
    align-items: stretch;
    flex-direction: column;
    gap: 0;
  }

  .hotnews-window .na-field {
    flex-basis: auto;
  }

  .hotnews-window__summary {
    margin-top: 0;
  }

  .hotnews-disclosure__hint {
    display: block;
    margin-left: 18px;
  }
}
</style>
