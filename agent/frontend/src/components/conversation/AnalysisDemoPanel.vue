<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { conversationsApi } from '@/api/conversations'
import type { ConversationRuntime } from '@/api/conversations'
import { hotNewsApi } from '@/api/hotNews'
import { ApiError } from '@/api/http'
import { ANALYSIS_EXAMPLES, AnalysisDemoPreparationError, enterpriseDemoCatalog, prepareAnalysisDemo } from '@/utils/analysisDemo'
import type { EnterpriseDemoCatalog } from '@/utils/analysisDemo'

const props = defineProps<{
  runtime: ConversationRuntime | null
  runtimeError?: string | null
  localDemoMode: boolean
  disabled: boolean
  contextKey: string
}>()
const emit = defineEmits<{
  example: [content: string]
  busy: [value: boolean]
  prepared: [conversationId: string, originContext: string]
}>()
const preparing = ref(false)
const progress = ref<string | null>(null)
const error = ref<string | null>(null)
const catalog = ref<EnterpriseDemoCatalog | null>(null)
const catalogLoading = ref(false)
const catalogError = ref<string | null>(null)
const catalogReady = ref(false)
const selectedScenarioId = ref('')
const selectedScenario = computed(() => catalog.value?.scenarios.find((item) => item.id === selectedScenarioId.value))
const scaleSummary = computed(() => catalog.value
  ? `每租户 ${catalog.value.newsPerTenant.toLocaleString('zh-CN')} 篇 · 共 ${catalog.value.totalNewsCount.toLocaleString('zh-CN')} 篇${catalog.value.datasetProfile === 'public-headlines-v3' ? '公开标题新闻记录' : '合成新闻'} · ${catalog.value.totalTenants} 个租户`
  : '')
const aggregateSummary = computed(() => catalog.value
  ? `每篇 ${catalog.value.hoursPerNews} 小时；指标 ${catalog.value.totalMetricRowCount.toLocaleString('zh-CN')} 条，基线 ${catalog.value.totalBaselineRowCount.toLocaleString('zh-CN')} 条（两租户合计）。`
  : '')
let catalogController: AbortController | null = null
let controller: AbortController | null = null
let sequence = 0
let disposed = false
let preparedContext: string | null = null
const analysisEnabled = computed(() => props.runtime?.data_analysis?.name === 'analyze_hot_news_data')
const preparationAuthorized = computed(() => props.localDemoMode && analysisEnabled.value
  && props.runtime?.model_provider === 'local' && props.runtime.query_enabled)
const unavailable = computed(() => !props.runtime
  ? props.runtimeError ? '无法确认数据分析权限，请刷新页面。' : '正在确认数据分析权限…'
  : !analysisEnabled.value ? '当前未启用数据分析。'
    : '点击操作填入草稿，确认后发送。')

function cancelPreparation(): void {
  ++sequence
  controller?.abort()
  controller = null
  if (preparing.value) {
    preparing.value = false
    emit('busy', false)
    if (!disposed) progress.value = '已停止准备；已开始的热点工作流或已保存的读取记录可能继续保留。'
  }
}

async function loadCatalog(): Promise<void> {
  catalogController?.abort()
  catalog.value = null
  catalogReady.value = false
  catalogError.value = null
  if (!preparationAuthorized.value || disposed) return
  const pending = new AbortController()
  catalogController = pending
  catalogLoading.value = true
  try {
    const result = enterpriseDemoCatalog(await hotNewsApi.localSqlConfig({ signal: pending.signal }))
    if (disposed || pending.signal.aborted || catalogController !== pending) return
    catalog.value = result
    selectedScenarioId.value = result?.scenarios.some((item) => item.id === selectedScenarioId.value)
      ? selectedScenarioId.value : result?.scenarios[0]?.id ?? ''
    catalogReady.value = true
  } catch {
    if (!disposed && !pending.signal.aborted) catalogError.value = '无法读取模拟情形目录，请检查演示服务后刷新目录。'
  } finally {
    if (catalogController === pending) { catalogLoading.value = false; catalogController = null }
  }
}

async function prepare(): Promise<void> {
  if (!preparationAuthorized.value || props.disabled || preparing.value || disposed
      || !catalogReady.value || catalogLoading.value || catalogError.value
      || catalog.value && !selectedScenario.value) return
  const origin = props.contextKey
  preparedContext = null
  const requestSequence = ++sequence
  const abortController = new AbortController()
  controller = abortController
  preparing.value = true
  error.value = null
  emit('busy', true)
  try {
    const prepared = await prepareAnalysisDemo({
      hotNews: hotNewsApi, conversations: conversationsApi, signal: abortController.signal,
      ...(catalog.value && selectedScenario.value ? {
        enterprise: { catalog: catalog.value, scenarioId: selectedScenario.value.id },
      } : {}),
      onProgress: (message) => {
        if (!disposed && sequence === requestSequence) progress.value = message
      },
    })
    if (disposed || sequence !== requestSequence || props.contextKey !== origin
        || abortController.signal.aborted) return
    progress.value = '两个来源已读取。请选择操作并发送；窗口可比性由实际分析校验。'
    preparedContext = prepared.conversationId
    preparing.value = false
    controller = null
    emit('busy', false)
    emit('prepared', prepared.conversationId, origin)
  } catch (cause) {
    if (disposed || sequence !== requestSequence || abortController.signal.aborted) return
    progress.value = null
    error.value = cause instanceof ApiError && cause.status === 409
      ? '演示数据状态冲突，请检查已保存运行和服务配置。未自动更换窗口或重试。'
      : cause instanceof AnalysisDemoPreparationError
        ? `${cause.message}（${cause.code}）`
        : '演示准备失败，请检查服务和授权。未自动重试；已完成的运行或会话可以从历史查看。'
  } finally {
    if (!disposed && sequence === requestSequence) {
      const wasPreparing = preparing.value
      preparing.value = false
      controller = null
      if (wasPreparing) emit('busy', false)
    }
  }
}
watch(() => props.contextKey, (context) => {
  cancelPreparation()
  if (context !== preparedContext) {
    progress.value = null
    error.value = null
  }
})
watch(preparationAuthorized, (authorized) => {
  if (!authorized) cancelPreparation()
  void loadCatalog()
}, { immediate: true })
onBeforeUnmount(() => { disposed = true; catalogController?.abort(); cancelPreparation() })
</script>

<template>
  <section class="na-analysis-demo" aria-label="数据分析操作">
    <h3>数据分析</h3>
    <p class="na-muted">{{ unavailable }}</p>
    <div class="na-analysis-demo__operations">
      <button
        v-for="example in ANALYSIS_EXAMPLES"
        :key="example.operation"
        class="na-btn"
        type="button"
        :disabled="!analysisEnabled || disabled || preparing"
        @click="emit('example', example.content)"
      >
        {{ example.label }}
      </button>
    </div>
    <div v-if="localDemoMode" class="na-analysis-demo__prepare">
      <template v-if="catalog">
        <p class="na-muted">{{ scaleSummary }}</p>
        <p class="na-muted">{{ aggregateSummary }}</p>
        <p v-if="catalog.datasetProfile === 'public-headlines-v3'" class="na-notice">
          标题采自公开报道（{{ catalog.headlineDateStart }} 至 {{ catalog.headlineDateEnd }}），指标仍为10月3日的模拟数据。
          标题发布日期与模拟指标窗口相互独立，不能解释为这些报道的真实用户行为。
        </p>
        <label class="na-analysis-demo__scenario">
          企业情形模拟
          <select v-model="selectedScenarioId" :disabled="disabled || preparing || catalogLoading" aria-label="选择企业模拟情形">
            <option v-for="scenario in catalog.scenarios" :key="scenario.id" :value="scenario.id">{{ scenario.label }}</option>
          </select>
        </label>
        <p v-if="selectedScenario" class="na-muted">{{ selectedScenario.description }}</p>
        <p v-if="selectedScenario" class="na-muted">每窗口取点击量 Top{{ selectedScenario.row_limit }} 候选，分析保存的完整榜单；两个固定窗口 · 新建会话。</p>
        <p class="na-muted">聊天新闻摘要展示前 5 篇；分析覆盖实际完整榜单，结果卡显示实际条数。</p>
      </template>
      <p v-else class="na-muted">{{ catalogLoading ? '正在读取模拟情形…' : '标准两窗口合成演示 · 新建独立会话' }}</p>
      <button class="na-btn" type="button" :disabled="!preparationAuthorized || disabled || preparing || !catalogReady || catalogLoading || !!catalogError || !!catalog && !selectedScenario" @click="prepare">
        {{ preparing ? '正在准备演示…' : catalog ? '准备所选情形' : '准备两窗口演示' }}
      </button>
      <button v-if="preparing" class="na-btn" type="button" @click="cancelPreparation">停止准备</button>
      <button v-if="catalogError" class="na-btn" type="button" :disabled="disabled || preparing" @click="loadCatalog">刷新目录</button>
      <p v-if="catalogError" class="na-muted" role="status">{{ catalogError }}</p>
      <p v-if="!preparationAuthorized" class="na-muted">需要本地模型、数仓查询授权与数据分析同时启用。</p>
    </div>
    <details class="na-analysis-demo__scope">
      <summary>{{ localDemoMode ? '使用范围与演示说明' : '使用范围' }}</summary>
      <p class="na-muted">基线使用已存参考字段；趋势需读取两个兼容窗口，实际可比性由分析校验。</p>
      <p v-if="localDemoMode" class="na-muted">
        {{ catalog ? '所选情形采用目录中的固定窗口与相同取数口径。' : '标准演示采用10月3日22–23时与23–24时。' }}
        准备会创建或复用两个合成热点运行，并在新会话读取来源。完成后由你发送分析问题。
        合成情形用于核对计算行为，不能证明企业采集完整性或模型质量。
      </p>
    </details>
    <p v-if="progress" class="na-muted" role="status">{{ progress }}</p>
    <p v-if="error" class="na-message na-message--error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.na-analysis-demo { margin: 12px 0; padding: 12px; border-top: 1px solid var(--na-border, #ddd); }
h3 { margin: 0 0 8px; font-size: 15px; }
.na-analysis-demo > p { margin: 6px 0; }
.na-analysis-demo__operations { display: flex; flex-wrap: wrap; gap: 6px; }
.na-analysis-demo__prepare { margin-top: 12px; }
.na-analysis-demo__prepare p { margin: 6px 0; }
.na-analysis-demo__prepare .na-btn + .na-btn { margin-left: 8px; }
.na-analysis-demo__scenario { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.na-analysis-demo__scenario select { max-width: 100%; padding: 6px; border: 1px solid var(--na-border); border-radius: 6px; background: var(--na-bg); color: var(--na-text); }
.na-analysis-demo__scope { margin-top: 8px; }
summary { cursor: pointer; color: var(--na-text-muted); }
@media (max-width: 600px) {
  .na-analysis-demo { padding: 10px 0; }
  .na-analysis-demo__operations { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .na-analysis-demo__operations .na-btn { padding: 6px 4px; }
}
</style>
