<script setup lang="ts">
import { onMounted, ref } from 'vue'

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
    simulationMessage.value = `热点 Agent 完成：上榜 ${result.ranked_news_count} 篇，分析 ${result.analyzed_news_count} 篇；SQL 工具记录 ${result.sql_query_id} 已绑定本次运行。`
  } catch (cause) {
    simulationError.value = describeError(cause)
  } finally {
    simulationRunning.value = false
  }
}

function prefillDecision(item: HotNewsRankedItemView): void {
  decisionNewsId.value = item.news_id
}

async function onDecisionRecorded(): Promise<void> {
  if (selectedRunId.value) await selectRun(selectedRunId.value)
}

onMounted(loadRuns)
</script>

<template>
  <HotNewsQueryTools
    v-if="localSimulationEnabled"
    :running="simulationRunning"
    @run="runLocalSimulation"
  />

  <p v-if="simulationMessage" class="na-message na-message--ok" role="status">
    {{ simulationMessage }}
  </p>
  <p v-if="simulationError" class="na-message na-message--error" role="alert">
    {{ simulationError }}
  </p>

  <section class="na-card">
    <div class="na-card__head">
      <h2>热点运营工作台</h2>
      <span class="na-badge na-badge--live">GET /hot-news/runs</span>
    </div>

    <p v-if="!appStore.hotNewsEnabled" class="na-notice">
      运行时配置中 <code>enableHotNews</code> 为 false，本模块在网关侧未开放。
    </p>

    <div class="na-row na-row--end">
      <button class="na-btn" type="button" :disabled="runsLoading" @click="loadRuns">
        {{ runsLoading ? '刷新中…' : '刷新运行列表' }}
      </button>
    </div>

    <NaStateBlock
      :loading="runsLoading && runs.length === 0"
      :error="runsError"
      :empty="runs.length === 0"
      empty-text="当前租户还没有已完成的热点运行"
      loading-text="加载热点运行…"
    >
      <table class="na-table">
        <thead>
          <tr>
            <th>窗口</th>
            <th>Production Bundle</th>
            <th style="width: 72px">上榜</th>
            <th style="width: 72px">已分析</th>
            <th>完成时间</th>
            <th style="width: 72px" />
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="run in runs"
            :key="run.run_id"
            :class="{ 'na-row--active': run.run_id === selectedRunId }"
          >
            <td class="na-mono">
              {{ formatDateTime(run.window_start) }} ~ {{ formatDateTime(run.window_end) }}
            </td>
            <td class="na-mono">{{ run.production_bundle_version }}</td>
            <td>{{ run.ranked_news_count }}</td>
            <td>{{ run.analyzed_news_count }}</td>
            <td>{{ formatDateTime(run.completed_at) }}</td>
            <td>
              <button
                class="na-btn"
                type="button"
                :disabled="simulationRunning"
                @click="selectRun(run.run_id)"
              >
                查看
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </NaStateBlock>
  </section>

  <HotNewsSqlToolTrace
    v-if="detail?.sql_tool_trace"
    :key="detail.run.run_id"
    :run-id="detail.run.run_id"
    :trace="detail.sql_tool_trace"
  />

  <section v-if="selectedRunId" class="na-card">
    <div class="na-card__head">
      <h2>榜单与热度分量</h2>
      <span v-if="detail" class="na-badge">{{ detail.run.status }}</span>
    </div>

    <NaStateBlock
      :loading="detailLoading"
      :error="detailError"
      :empty="!detail"
      empty-text="选择一次运行查看榜单"
      loading-text="加载榜单…"
    >
      <template v-if="detail">
        <p class="na-muted">
          窗口 {{ formatDateTime(detail.run.window_start) }} ~
          {{ formatDateTime(detail.run.window_end) }} · 行为记录
          {{ detail.run.fetched_record_count }} 条 · 指标快照
          {{ detail.run.metric_snapshot_count }} 篇 · 数字均来自后端确定性计算，可追溯到
          analysis_runs 快照。
        </p>

        <p v-if="detail.sql_tool_trace" class="na-muted">
          本榜单使用上方同一次 SQL 工具调用的候选新闻与补充指标，由 Python HotNewsRanker
          计算最终热度；顺序可能与 SQL 候选筛选结果不同。
        </p>

        <p v-if="detail.analysis_execution" class="na-muted">
          模型分析 {{ detail.analysis_execution.task_count }} 篇 · 本轮并发峰值
          {{ detail.analysis_execution.observed_concurrency }} · Worker 并发上限
          {{ detail.analysis_execution.max_concurrency }} · 分析阶段耗时
          {{ detail.analysis_execution.elapsed_ms }} 毫秒（含等待并发名额）
        </p>

        <HotNewsRankTable :items="detail.ranked_news" @decide="prefillDecision" />
      </template>
    </NaStateBlock>
  </section>

  <section v-if="detail && detail.decisions.length" class="na-card">
    <div class="na-card__head">
      <h2>本次运行的决策记录</h2>
    </div>
    <table class="na-table">
      <thead>
        <tr>
          <th>news_id</th>
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
  </section>

  <HotNewsDecisionForm
    v-if="selectedRunId"
    :run-id="selectedRunId"
    :news-id="decisionNewsId"
    @recorded="onDecisionRecorded"
  />
</template>
