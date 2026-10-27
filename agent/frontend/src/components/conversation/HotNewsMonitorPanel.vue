<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { hotNewsMonitorApi } from '@/api/hotNewsMonitor'
import type { MonitorFeed, MonitorView } from '@/api/hotNewsMonitor'

const feed = ref<MonitorFeed | null>(null)
const error = ref('')
const operationError = ref('')
const busy = ref(false)
const minutes = ref<5 | 15>(5)
const clock = ref(Date.now())
let disposed = false
let sequence = 0
let stopStream: (() => void) | null = null
let requestController: AbortController | null = null
let timer: ReturnType<typeof setInterval> | null = null

const cards = computed(() => feed.value?.source_state === 'ready'
  ? feed.value.items.filter(item => item.window_end_ms <= clock.value && item.expires_at_ms > clock.value) : [])
const active = computed(() => feed.value?.monitors.some(item => item.enabled) ?? false)
const summary = computed(() => {
  if (error.value || feed.value?.source_state === 'unavailable') return '连接暂时中断，正在重新获取热点。'
  if (!active.value) return '选择时间窗口，持续发现达到热度门槛的新闻。'
  if (feed.value?.monitors.some(item => item.enabled && item.status === 'blocked')) return '部分监控已阻塞，请检查数据覆盖与采集状态。'
  if (feed.value?.source_state === 'waiting_source') return '监控已登记，等待实时数据源。'
  if (!cards.value.length) return '等待完整窗口的热点结果；暂无可展示的新热点。'
  return '窗口每分钟推进，分析完成后自动更新。'
})

function windowLabel(start: number, end: number): string {
  const format = (stamp: number) => new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(stamp)
  return format(start) + ' — ' + format(end) + '（北京时间）'
}
function statusLabel(item: MonitorView): string {
  return { waiting_source: '等待数据', published: '监控中', paused: '已暂停', blocked: '已阻塞', backpressure: '等待分析队列' }[item.status]
}
function disconnect(): void {
  sequence++
  stopStream?.()
  stopStream = null
  requestController?.abort()
  requestController = null
}
function accept(value: MonitorFeed): void {
  clock.value = Date.now()
  feed.value = value
  error.value = ''
}
async function connect(): Promise<void> {
  disconnect()
  if (disposed || document.hidden) return
  const current = sequence
  const controller = new AbortController()
  requestController = controller
  try {
    const result = await hotNewsMonitorApi.snapshot(controller.signal)
    if (disposed || current !== sequence) return
    accept(result)
    if (!result.configured) return
    stopStream = hotNewsMonitorApi.subscribe(value => {
      if (!disposed && current === sequence) accept(value)
    }, () => {
      if (disposed || current !== sequence) return
      error.value = '连接中断'
      if (feed.value) feed.value = { ...feed.value, source_state: 'unavailable', items: [] }
    })
  } catch {
    if (!disposed && current === sequence) {
      error.value = '无法获取监控状态'
      feed.value = null
    }
  } finally {
    if (requestController === controller) requestController = null
  }
}
async function change(action: () => Promise<MonitorFeed>): Promise<void> {
  if (busy.value || !feed.value?.can_manage) return
  busy.value = true
  operationError.value = ''
  disconnect() // A late pre-mutation read must not undo the confirmed new state.
  try {
    const result = await action()
    if (!disposed) accept(result)
  } catch {
    if (!disposed) operationError.value = '操作结果待确认，请刷新状态；若没有运行中的数据源，请先开启采集。'
  } finally {
    busy.value = false
    // Read after uncertain outcomes; never retry the mutation automatically.
    if (!disposed) await connect()
  }
}
function start(): Promise<void> { return change(() => hotNewsMonitorApi.start(minutes.value)) }
function toggle(item: MonitorView): Promise<void> { return change(() => hotNewsMonitorApi.switch(item.monitor_id, !item.enabled)) }
function visibilityChanged(): void {
  if (document.hidden) disconnect()
  else if (!busy.value) void connect()
}
onMounted(() => {
  void connect()
  timer = setInterval(() => {
    clock.value = Date.now()
    // Also recover an initial failed GET. SSE owns normal reconnects.
    if (error.value && !stopStream && !requestController && !busy.value && !document.hidden) void connect()
  }, 5000)
  document.addEventListener('visibilitychange', visibilityChanged)
})
onBeforeUnmount(() => {
  disposed = true
  disconnect()
  if (timer !== null) clearInterval(timer)
  document.removeEventListener('visibilitychange', visibilityChanged)
})
</script>

<template>
  <section v-if="feed?.configured || error" class="na-hot-monitor" aria-label="持续热点">
    <div class="na-hot-monitor__heading">
      <div><h3>持续热点</h3><p class="na-muted">{{ summary }}</p></div>
      <div v-if="feed?.can_manage" class="na-hot-monitor__controls">
        <select v-model="minutes" :disabled="busy" aria-label="热点统计窗口">
          <option :value="5">最近 5 分钟</option><option :value="15">最近 15 分钟</option>
        </select>
        <button class="na-btn" type="button" :disabled="busy" @click="start">开启监控</button>
      </div>
    </div>
    <p v-if="operationError" class="na-message na-message--error" role="alert">{{ operationError }}</p>
    <button v-if="error || operationError" class="na-btn" type="button" :disabled="busy" @click="connect">刷新状态</button>
    <div v-if="feed?.monitors.length" class="na-hot-monitor__states">
      <span v-for="item in feed.monitors" :key="item.monitor_id">
        {{ item.window_minutes }} 分钟 · {{ statusLabel(item) }}
        <button v-if="feed.can_manage" type="button" :disabled="busy" @click="toggle(item)">{{ item.enabled ? '暂停' : '恢复' }}</button>
      </span>
    </div>
    <div class="na-hot-monitor__cards">
      <article v-for="item in cards" :key="item.notice_id" class="na-hot-monitor__card">
        <small>{{ windowLabel(item.window_start_ms, item.window_end_ms) }}</small>
        <h4>{{ item.title }}</h4>
        <p>热度 {{ item.hot_score.toFixed(3) }} · 点击 {{ item.clicks }} · 点击人数 {{ item.unique_users }}</p>
        <details>
          <summary>查看分析</summary>
          <p>{{ item.trend_assessment }}</p>
          <small>规则 {{ item.policy_version }} · 新闻 {{ item.news_id }}</small>
        </details>
      </article>
    </div>
    <small class="na-muted">公开新闻 · 模拟行为 · 本地模型分析</small>
  </section>
</template>

<style scoped>
.na-hot-monitor { width: 100%; max-width: 800px; margin: 0 auto; padding: 16px; border: 1px solid var(--na-border, #dce3dc); border-radius: 12px; background: var(--na-bg, #fafcf9); text-align: left; }
.na-hot-monitor__heading { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; align-items: center; }
h3 { margin: 0; font-size: 15px; }
.na-hot-monitor__heading p { margin: 6px 0; font-size: 12px; }
.na-hot-monitor__controls, .na-hot-monitor__states { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
select { border: 1px solid var(--na-border); border-radius: 7px; padding: 8px; background: white; font: inherit; font-size: 12px; }
.na-hot-monitor__states { margin: 10px 0; font-size: 12px; }
.na-hot-monitor__states span { padding: 5px 8px; border-radius: 6px; background: #edf3ed; }
.na-hot-monitor__states button { margin-left: 8px; border: 0; background: none; color: var(--na-accent, #326444); cursor: pointer; }
.na-hot-monitor__cards { display: grid; gap: 10px; margin: 12px 0; }
.na-hot-monitor__card { padding: 12px; background: white; border: 1px solid var(--na-border); border-radius: 8px; overflow-wrap: anywhere; }
.na-hot-monitor__card small { color: var(--na-text-muted, #657269); font-size: 11px; }
h4 { margin: 8px 0; font-size: 14px; line-height: 1.6; }
.na-hot-monitor__card p { font-size: 12px; line-height: 1.8; white-space: pre-wrap; }
summary { font-size: 12px; color: var(--na-accent, #326444); cursor: pointer; }
</style>

