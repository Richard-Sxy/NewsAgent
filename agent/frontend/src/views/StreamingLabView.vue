<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { LAB_TENANTS, METRIC_LABELS, streamingLab } from '@/api/streamingLab'
import type { LabRun, LabRunSummary, LabOperations, MetricKey, LiveNews } from '@/api/streamingLab'
import { consumptionLabel, metricDelta, previousRevision, windowRevisions } from '@/utils/streamingLab'
import { formatDecimal } from '@/utils/format'

const route = useRoute()
const runs = ref<LabRunSummary[]>([])
const runId = ref(typeof route.query.run === 'string' ? route.query.run : '')
const tenant = ref(LAB_TENANTS[0])
const data = ref<LabRun | null>(null)
const windowKey = ref('')
const snapshotId = ref('')
const loading = ref(false)
const error = ref('')
const refreshed = ref('')
const operations = ref<LabOperations | null>(null)
const operationsError = ref('')
const liveNews = ref<LiveNews[]>([])
const liveError = ref('')
const topicNames = ref<Record<string, string>>({})
let liveController: AbortController | undefined
const autoRefresh = ref(true)
const followLive = ref(!route.query.run)
let timer: ReturnType<typeof setInterval> | undefined
let pollBusy = false
let disposed = false
let operationsController: AbortController | undefined
const keys = Object.keys(METRIC_LABELS) as MetricKey[]
let controller: AbortController | undefined
let listController: AbortController | undefined
const windows = computed(() => [...new Set((data.value?.snapshots ?? []).map((r) => r.window_key))]
  .sort((a, b) => Number(b.split('/').at(-1)) - Number(a.split('/').at(-1))))
const revisions = computed(() => windowRevisions(data.value?.snapshots ?? [], tenant.value, windowKey.value))
const selected = computed(() => revisions.value.find((r) => r.snapshot_id === snapshotId.value) ?? revisions.value[0])
const previous = computed(() => selected.value ? previousRevision(revisions.value, selected.value) : undefined)
const labels: Record<string, string> = {
  'synthetic-article-1': '城市公共交通服务调整',
  'synthetic-video-2': '科学展览视频导览',
  'synthetic-article-3': '社区阅读活动开放报名',
}
function time(ms: number) { return new Date(ms).toLocaleString('zh-CN', { hour12: false }) }
function delta(news: string, key: MetricKey) {
  if (!selected.value) return ''
  const value = metricDelta(selected.value, previous.value, news, key)
  return value === null ? '—' : value > 0 ? `+${value}` : String(value)
}
async function refresh() {
  controller?.abort()
  const own = new AbortController()
  controller = own
  data.value = null
  error.value = ''
  loading.value = true
  if (!runId.value) { loading.value = false; return }
  try {
    const result = await streamingLab.run(runId.value, tenant.value, own.signal)
    if (controller !== own || own.signal.aborted) return
    data.value = result
    if (!windows.value.includes(windowKey.value)) windowKey.value = windows.value[0] ?? ''
    if (!result.snapshots.some((r) => r.snapshot_id === snapshotId.value)) snapshotId.value = ''
    refreshed.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } catch (cause) {
    if (!own.signal.aborted && controller === own) error.value = cause instanceof Error ? cause.message : '查询失败'
  } finally { if (controller === own) loading.value = false }
}
async function refreshRuns() {
  await refreshOperations()
  await refreshLive()
  if (disposed) return
  listController?.abort()
  const own = new AbortController()
  listController = own
  try {
    const result = await streamingLab.runs(own.signal)
    if (listController !== own || own.signal.aborted) return
    runs.value = result.runs
    const live = liveNews.value[0]?.run_id ?? operations.value?.dynamic_feed?.recent_batches[0]?.run_id
    if (followLive.value && live && live !== runId.value) runId.value = live
    else if (!runId.value) runId.value = runs.value[0]?.run_id ?? ''
    else await refresh()
  } catch (cause) {
    if (!own.signal.aborted) error.value = cause instanceof Error ? cause.message : '读取运行列表失败'
  }
}
async function refreshLive() {
  liveController?.abort()
  const own = new AbortController()
  liveController = own
  liveError.value = ''
  try {
    const result = await streamingLab.realtime(tenant.value, own.signal)
    if (own.signal.aborted || liveController !== own) return
    liveNews.value = result.items
    topicNames.value = result.topic_names ?? {}
    for (const row of result.items) labels[row.news_id] = row.title
  } catch (cause) {
    if (!own.signal.aborted) {
      liveNews.value = []
      liveError.value = cause instanceof Error ? cause.message : '实时窗口读取失败'
    }
  }
}
async function refreshOperations() {
  operationsController?.abort()
  const own = new AbortController()
  operationsController = own
  operations.value = null
  operationsError.value = ''
  try {
    const value = await streamingLab.operations(own.signal)
    if (!own.signal.aborted && operationsController === own) operations.value = value
  } catch (cause) {
    if (!own.signal.aborted) operationsError.value = cause instanceof Error ? cause.message : '运行状态不可用'
  }
}
watch([runId, tenant], () => { windowKey.value = ''; snapshotId.value = ''; void refresh() })
watch(tenant, () => { liveNews.value = []; void refreshLive() })
watch(windowKey, () => { snapshotId.value = '' })
async function poll() {
  if (!autoRefresh.value || pollBusy || loading.value || document.hidden) return
  pollBusy = true
  try { await refreshRuns() } finally { pollBusy = false }
}
onMounted(() => { void refreshRuns(); timer = setInterval(() => { void poll() }, 5000) })
onUnmounted(() => { disposed = true; clearInterval(timer); controller?.abort(); listController?.abort(); operationsController?.abort(); liveController?.abort() })
</script>

<template>
  <section class="stream-lab" aria-labelledby="stream-title">
    <header class="stream-heading">
      <div><h1 id="stream-title">流式快照工作台</h1><p class="na-muted">查看窗口指标、修订差异和每个版本的分析结果。</p></div>
      <button type="button" :disabled="loading" @click="refreshRuns">{{ loading ? '读取中…' : '刷新数据' }}</button>
    </header>
    <p class="stream-notice">公开新闻 · 合成读者行为 · 按电脑时间逐秒供数。实时结果每 5 秒刷新，一分钟窗口保留 30 秒迟到预算。本地模型仅验证调用链。</p>
    <div class="stream-toggles">
      <label><input v-model="autoRefresh" type="checkbox"> 每 5 秒刷新</label>
      <label><input v-model="followLive" type="checkbox"> 跟随最新动态窗口</label>
    </div>
    <div v-if="operations?.wallclock" class="stream-card" aria-label="真实时钟实时数仓">
      <h2>实时访问与用户兴趣</h2>
      <p>{{ operations.wallclock.enabled ? '正在逐秒供数' : '供数已暂停' }} · 画像消费者落后 {{ operations.wallclock.reader_lag_seconds === null ? '未知' : formatDecimal(operations.wallclock.reader_lag_seconds, 1) + ' 秒' }}</p>
      <p v-if="operations.wallclock.last_error" class="stream-error" role="alert">{{ operations.wallclock.last_error }}</p>
      <p v-if="liveError" class="stream-error" role="alert">{{ liveError }}</p>
      <p class="na-muted">详情访问按用户去重。兴趣来自窗口开始前的行为，并排除当前新闻。初始 30 天历史为合成种子；低样本不展示 Lift，未知画像不补零。</p>
      <div class="stream-table live-news-table"><table>
        <thead><tr><th>新闻 / 窗口</th><th>曝光 / 点击</th><th>详情读者</th><th>近 24 小时兴趣</th><th>近 30 天兴趣</th><th>状态</th></tr></thead>
        <tbody><tr v-for="row in liveNews" :key="row.window_key + row.news_id">
          <td>{{ row.title }}<br><small>{{ new Date(row.window_start).toLocaleTimeString('zh-CN') }}</small></td>
          <td>{{ row.impressions }} / {{ row.clicks }}</td><td>{{ row.reader_count ?? '未知' }}</td>
          <td><template v-if="row.reader_profile">已画像 {{ row.reader_profile.short_term.profiled_reader_count }} / 未知 {{ row.reader_profile.short_term.unknown_reader_count }}<br>
            <span v-for="topic in row.reader_profile.short_term.topics.slice(0, 3)" :key="topic.topic_id">{{ topicNames[topic.topic_id] ?? topic.topic_id }} {{ topic.reader_count }} 人；</span></template></td>
          <td><template v-if="row.reader_profile">已画像 {{ row.reader_profile.long_term.profiled_reader_count }} / 未知 {{ row.reader_profile.long_term.unknown_reader_count }}<br>
            <span v-for="topic in row.reader_profile.long_term.topics.slice(0, 3)" :key="topic.topic_id">{{ topicNames[topic.topic_id] ?? topic.topic_id }} {{ topic.reader_count }} 人；</span></template></td>
          <td>{{ row.final ? '窗口已终结' : '实时修订' }} · v{{ row.revision }}<br>{{ row.source_check_fresh ? '数据源正常' : '数据源可能中断' }}</td>
        </tr></tbody>
      </table></div>
    </div>
    <div v-if="operations?.dynamic_feed && !operations?.wallclock" class="stream-card" aria-label="动态数据源">
      <h2>动态模拟数仓</h2>
      <p>{{ operations.dynamic_feed.enabled ? '已启用自动生成' : '已暂停自动生成' }} · 每 {{ operations.dynamic_feed.interval_seconds }} 秒一轮 · 已分配 {{ operations.dynamic_feed.sequence }} / {{ operations.dynamic_feed.max_windows }} 个窗口。</p>
      <p class="na-muted">固定三条虚构新闻轮流升温、突增和回落；包含重复访问、事件乱序与重传。此处为批次加速模拟，事件时间与电脑时钟不同。</p>
      <p v-if="operations.dynamic_feed.last_error" role="alert" class="stream-error">生成已停止：{{ operations.dynamic_feed.last_error }}。请检查持久任务与资源预算。</p>
      <div class="stream-table">
        <table>
          <thead><tr><th>轮次</th><th>主情景</th><th>投放记录（含重传）</th><th>链路状态</th></tr></thead>
          <tbody>
            <tr v-for="batch in operations.dynamic_feed.recent_batches.slice(0, 6)" :key="batch.run_id">
              <td><button type="button" @click="followLive = false; runId = batch.run_id">{{ batch.run_id }}</button></td>
              <td>{{ ({ warming: '预热', rising: '升温', breaking: '突增', peak: '高峰', cooling: '回落', long_tail: '长尾' } as Record<string, string>)[batch.scenario] ?? batch.scenario }}</td>
              <td>{{ batch.event_count }}</td><td>{{ batch.state === 'completed' ? '聚合、分析、核对完成' : '处理中' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
    <div v-if="operations" class="stream-summary" aria-label="实验运行状态">
      <div><small>消费调度</small><strong>{{ operations.dispatcher.state === 'ready' ? '运行正常' : operations.dispatcher.state === 'recovering' ? '等待依赖恢复' : '状态待确认' }}</strong></div>
      <div><small>等待 / 分析中 / 有效窗口失败</small><strong>{{ operations.ledger.pending }} / {{ operations.ledger.started }} / {{ operations.ledger.failed - (operations.invalidated_failures ?? 0) }}</strong></div>
      <div><small>最久等待</small><strong>{{ formatDecimal(operations.oldest_pending_seconds, 1) }} 秒</strong></div>
      <div><small>全部已保存报告</small><strong>{{ operations.publication_to_report.report_count }}</strong></div>
    </div>
    <p v-if="operations?.invalidated_failures" class="na-muted">另有 {{ operations.invalidated_failures }} 个历史任务因输入窗口失效而停止，保留供审计；不计入有效窗口失败。</p>
    <p v-if="operations?.dispatcher.error_type" role="status" class="stream-notice">依赖恢复中：{{ operations.dispatcher.error_type }}，连续失败 {{ operations.dispatcher.consecutive_failures }} 次；任务保留在消费账本中。</p>
    <p v-if="operationsError" role="alert" class="stream-error">{{ operationsError }}。运行状态未知，不能据此判断任务已完成。</p>
    <div class="stream-filters">
      <label>运行<select v-model="runId" aria-label="运行" @change="followLive = false"><option v-if="runId && !runs.some(r => r.run_id === runId)" :value="runId">{{ runId }}</option><option v-for="run in runs" :key="run.run_id" :value="run.run_id">{{ run.run_id }}</option></select></label>
      <label>租户<select v-model="tenant" aria-label="租户"><option v-for="(id, i) in LAB_TENANTS" :key="id" :value="id">合成租户 {{ i === 0 ? 'A' : 'B' }}</option></select></label>
      <label>事件时间窗口<select v-model="windowKey" aria-label="事件时间窗口"><option v-for="key in windows" :key="key" :value="key">{{ time(Number(key.split('/').at(-1))) }} · 60 秒</option></select></label>
    </div>
    <p v-if="error" role="alert" class="stream-error">{{ error }}。当前结果已清空，请刷新重试。</p>
    <p v-else-if="loading" role="status">正在读取快照与分析账本…</p>
    <p v-else-if="!selected" role="status">{{ liveNews.length ? '当前分钟正在聚合，上方展示实时数据；窗口结束后生成分析快照。' : '暂无快照，请等待供数或选择已有运行和租户。' }}</p>
    <template v-if="selected && !loading && !error">
      <div class="stream-summary">
        <label>快照版本<select v-model="snapshotId" aria-label="快照版本"><option value="">最新版本</option><option v-for="row in revisions" :key="row.snapshot_id" :value="row.snapshot_id">v{{ row.revision }} · {{ row.base_snapshot_id ? '超期补数' : row.final ? '窗口终结' : '实时修订' }} · {{ consumptionLabel(row) }}</option></select></label>
        <div><small>当前版本</small><strong>v{{ selected.revision }} · {{ selected.is_latest ? '最新' : '历史' }}</strong></div>
        <div><small>去重事件</small><strong>{{ selected.snapshot.event_count }}</strong></div>
        <div><small>消费状态</small><strong>{{ consumptionLabel(selected) }}</strong></div>
      </div>
      <p v-if="!selected.is_latest" class="stream-notice">正在查看历史版本；该报告仍对应当时的快照，请切换“最新版本”查看当前结果。</p>
      <p v-if="selected.invalidated" role="alert" class="stream-error">窗口存在冲突事件，此版本及其历史分析均已失效，不可作为当前结果使用。</p>
      <p v-if="selected.error_type" role="alert" class="stream-error">分析未完成：{{ selected.error_type }}</p>
      <p v-if="selected.base_snapshot_id">超期补数新增 {{ selected.added_event_count }} 个事件 · {{ selected.backfill_reason }}。已对完整窗口重新去重、计算人数。</p>
      <div class="stream-card">
        <h2>指标与版本差异</h2><p class="na-muted">{{ previous ? `括号内为相对 v${previous.revision} 的变化` : '首个版本，无可比较的上一个版本' }} · 数据更新时间 {{ refreshed }}</p>
        <div class="stream-table"><table><thead><tr><th>新闻</th><th v-for="key in keys" :key="key">{{ METRIC_LABELS[key] }}</th></tr></thead><tbody><tr v-for="row in selected.snapshot.metrics" :key="row.news_id"><th>{{ labels[row.news_id] ?? row.news_id }}</th><td v-for="key in keys" :key="key">{{ row[key] }} <small class="na-muted">({{ delta(row.news_id, key) }})</small></td></tr></tbody></table></div>
      </div>
      <div v-if="selected.report && selected.state === 'completed' && !selected.invalidated" class="stream-card">
        <h2>热点排行与分析</h2>
        <article v-for="rank in selected.report.ranking" :key="rank.news_id" class="stream-analysis">
          <h3>{{ rank.rank }}. {{ labels[rank.news_id] ?? rank.news_id }} <span class="na-muted" :title="rank.hot_score">· 热度 {{ formatDecimal(rank.hot_score, 4) }}</span></h3>
          <p>{{ selected.report.analyses.find(a => a.news_id === rank.news_id)?.report.trend_assessment ?? '无分析文本' }}</p>
        </article>
        <ul><li v-for="item in selected.report.limitations" :key="item">{{ item }}</li></ul>
      </div>
      <p v-else-if="!selected.invalidated" role="status">当前版本尚无可用分析报告。刷新可查看后续消费结果。</p>
      <details class="stream-card">
        <summary>版本与调用证据</summary><dl>
          <dt>快照 SHA256</dt><dd>{{ selected.snapshot_id }}</dd>
          <template v-if="selected.base_snapshot_id"><dt>补数基础快照</dt><dd>{{ selected.base_snapshot_id }}</dd><dt>补数请求</dt><dd>{{ selected.backfill_request_id }}</dd></template>
          <dt>指标版本</dt><dd>{{ selected.snapshot.metric_version }}</dd><dt>Workflow</dt><dd>{{ selected.workflow_id }}</dd>
          <dt>调度证据</dt><dd><a :href="`/streaming-lab-api/snapshots/${selected.snapshot_id}/dispatch`" target="_blank" rel="noopener noreferrer">查看本版本的提交与状态记录</a></dd>
          <template v-if="selected.report"><dt>分析运行</dt><dd>{{ selected.report.run_key }}</dd><dt>分析 Trace</dt><dd><a :href="`http://127.0.0.1:23200/api/traces/${selected.report.trace_id}`" target="_blank" rel="noopener noreferrer">{{ selected.report.trace_id }}</a>（临时观测存储可能过期）</dd></template>
        </dl>
      </details>
    </template>
    <details v-if="data?.rejections.length" class="stream-card"><summary>被拒收的事件（{{ data.rejections.length }}{{ data.rejections_truncated ? '+' : '' }}）</summary><ul><li v-for="(item, index) in data.rejections" :key="index">{{ item.reason }} · {{ item.event_id }}</li></ul></details>
  </section>
</template>

<style scoped>
.stream-lab { display: grid; gap: 18px; }
.stream-heading, .stream-summary { display: flex; align-items: center; justify-content: space-between; gap: 18px; flex-wrap: wrap; }
.stream-heading h1 { font-size: 24px; }
.stream-heading p, .stream-card p { margin: 6px 0; }
.stream-notice { padding: 12px 16px; margin: 0; background: var(--na-accent-weak); border-radius: 8px; }
.stream-filters { display: grid; grid-template-columns: minmax(240px, 1.4fr) 1fr 1.2fr; gap: 16px; }
label { display: grid; gap: 6px; color: var(--na-text-muted); }
.stream-toggles { display: flex; gap: 24px; flex-wrap: wrap; }
.stream-toggles label { display: flex; align-items: center; gap: 8px; }
select, button { border: 1px solid var(--na-border-strong); background: white; border-radius: 8px; padding: 9px 12px; color: var(--na-text); max-width: 100%; }
button { cursor: pointer; color: var(--na-accent); }
button:disabled { opacity: .6; }
.stream-summary { padding: 18px; background: var(--na-surface); border: 1px solid var(--na-border); border-radius: 12px; }
.stream-summary small, .stream-summary strong { display: block; }
.stream-summary strong { font-size: 19px; }
.stream-card { background: white; padding: 20px; border: 1px solid var(--na-border); border-radius: 12px; min-width: 0; }
.stream-table { overflow-x: auto; }
.live-news-table table { table-layout: fixed; min-width: 860px; white-space: normal; }
.live-news-table th:first-child { width: 28%; }
.live-news-table th:nth-child(2) { width: 12%; }
.live-news-table th:nth-child(3) { width: 8%; }
.live-news-table th:nth-child(4), .live-news-table th:nth-child(5) { width: 19%; }
.live-news-table th:last-child { width: 14%; }
.live-news-table td { vertical-align: top; overflow-wrap: anywhere; }
table { border-collapse: collapse; width: 100%; margin-top: 12px; text-align: left; white-space: nowrap; }
th, td { padding: 12px 10px; border-bottom: 1px solid var(--na-border); }
td { font-variant-numeric: tabular-nums; }
.stream-analysis { padding: 14px 0; border-bottom: 1px solid var(--na-border); }
.stream-error { color: var(--na-danger); background: var(--na-danger-weak); padding: 12px; border-radius: 8px; }
dd { margin: 0 0 12px; overflow-wrap: anywhere; font-family: var(--na-mono); font-size: 12px; }
dt { color: var(--na-text-muted); }
summary { cursor: pointer; }
@media (max-width: 900px) { .stream-filters { grid-template-columns: 1fr; } }
</style>
