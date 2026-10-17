<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import { audienceApi } from '@/api/audience'
import type { AudiencePreview, AudienceSelection, AudienceTopicCatalog } from '@/api/audience'
import { ApiError } from '@/api/http'
import { formatDateTime, formatDecimal } from '@/utils/format'

const props = defineProps<{ runId: string; newsId: string }>()
const catalog = ref<AudienceTopicCatalog | null>(null)
const includeTopicIds = ref<string[]>([])
const excludeTopicIds = ref<string[]>([])
const match = ref<'any' | 'all'>('any')
const preview = ref<AudiencePreview | null>(null)
const catalogPending = ref(false)
const previewPending = ref(false)
const catalogError = ref('')
const previewError = ref('')
const selectionError = ref('')
const previewDenied = ref(false)
const now = ref(Date.now())
let catalogSequence = 0
let previewSequence = 0
let catalogController: AbortController | null = null
let previewController: AbortController | null = null
let expiryTimer: ReturnType<typeof setTimeout> | undefined

const groups = computed(() => (catalog.value?.topics ?? [])
  .filter((topic) => topic.parent_topic_id === null)
  .map((topic) => ({ root: topic, children: catalog.value!.topics.filter((child) => child.parent_topic_id === topic.topic_id) })))
const selection = computed<AudienceSelection>(() => ({
  taxonomy_version: catalog.value?.taxonomy_version ?? '',
  include_topic_ids: [...includeTopicIds.value].sort(),
  exclude_topic_ids: [...excludeTopicIds.value].sort(),
  match: match.value,
}))
const expired = computed(() => preview.value !== null && now.value >= Date.parse(preview.value.snapshot.expires_at))
const canPreview = computed(() => catalog.value !== null && includeTopicIds.value.length > 0
  && includeTopicIds.value.length <= 10 && excludeTopicIds.value.length <= 10
  && !catalogPending.value && !previewPending.value && !previewDenied.value)

function invalidatePreview(): void {
  previewSequence++
  previewController?.abort()
  previewController = null
  clearTimeout(expiryTimer)
  preview.value = null
  previewPending.value = false
  previewError.value = ''
}

function refreshExpiry(): void {
  clearTimeout(expiryTimer)
  now.value = Date.now()
  if (preview.value && !expired.value) {
    expiryTimer = setTimeout(refreshExpiry, Math.min(Date.parse(preview.value.snapshot.expires_at) - now.value + 1, 2_147_483_647))
  }
}

function failureMessage(cause: unknown, operation: 'catalog' | 'preview'): string {
  if (cause instanceof ApiError) {
    if (cause.status === 401) return '登录状态已失效，请重新登录后刷新。'
    if (cause.status === 403) return operation === 'catalog' ? '当前账号没有查看主题目录的权限。' : '当前账号没有受众预估权限，需要运营决策权限。'
    if (cause.status === 404) return '当前新闻或分析运行不可用，请刷新热点榜单。'
    if (cause.status === 409 || cause.status === 422) return '主题目录或所选范围已变化，请重新加载目录后选择。'
    if (cause.status === 429) return '预估请求较多，请稍后再试。'
    if (cause.status === 503) return '受众预估服务尚未启用或暂不可用，仍可查看原热点分析。'
  }
  return operation === 'catalog' ? '主题目录暂时无法加载，请稍后重试。' : '受众预估暂时无法完成，请稍后重试。'
}

async function loadCatalog(): Promise<void> {
  invalidatePreview()
  const sequence = ++catalogSequence
  catalogController?.abort()
  const controller = new AbortController()
  catalogController = controller
  catalog.value = null
  includeTopicIds.value = []
  excludeTopicIds.value = []
  selectionError.value = ''
  previewDenied.value = false
  catalogError.value = ''
  catalogPending.value = true
  try {
    const value = await audienceApi.topics(controller.signal)
    if (sequence === catalogSequence && !controller.signal.aborted) catalog.value = value
  } catch (cause) {
    if (sequence === catalogSequence && !controller.signal.aborted) catalogError.value = failureMessage(cause, 'catalog')
  } finally {
    if (sequence === catalogSequence) catalogPending.value = false
  }
}

function toggleTopic(topicId: string, dimension: 'include' | 'exclude'): void {
  const topic = catalog.value?.topics.find((item) => item.topic_id === topicId)
  const parent = topic?.parent_topic_id ? catalog.value?.topics.find((item) => item.topic_id === topic.parent_topic_id) : null
  if (!topic?.enabled || (parent && !parent.enabled)) return
  const chosen = dimension === 'include' ? includeTopicIds : excludeTopicIds
  const other = dimension === 'include' ? excludeTopicIds : includeTopicIds
  selectionError.value = ''
  if (!chosen.value.includes(topicId) && chosen.value.length >= 10) {
    selectionError.value = '纳入和排除范围各最多选择10个主题，请先减少已选主题。'
    return
  }
  chosen.value = chosen.value.includes(topicId) ? chosen.value.filter((id) => id !== topicId) : [...chosen.value, topicId]
  other.value = other.value.filter((id) => id !== topicId)
}

async function estimateAudience(): Promise<void> {
  if (!canPreview.value || !props.runId || !props.newsId) return
  const body = selection.value
  invalidatePreview()
  const sequence = previewSequence
  const controller = new AbortController()
  previewController = controller
  previewPending.value = true
  try {
    const value = await audienceApi.preview(props.runId, props.newsId, body, controller.signal)
    if (sequence !== previewSequence || controller.signal.aborted) return
    preview.value = value
    refreshExpiry()
  } catch (cause) {
    if (sequence !== previewSequence || controller.signal.aborted) return
    previewDenied.value = cause instanceof ApiError && cause.status === 403
    previewError.value = failureMessage(cause, 'preview')
  } finally {
    if (sequence === previewSequence) previewPending.value = false
  }
}

watch([includeTopicIds, excludeTopicIds, match], invalidatePreview, { deep: true, flush: 'sync' })
watch(() => [props.runId, props.newsId], () => {
  invalidatePreview()
  catalogSequence++
  catalogController?.abort()
  catalog.value = null
  catalogPending.value = false
  catalogError.value = ''
  previewDenied.value = false
  includeTopicIds.value = []
  excludeTopicIds.value = []
  selectionError.value = ''
  match.value = 'any'
}, { flush: 'sync' })
onBeforeUnmount(() => {
  invalidatePreview()
  catalogSequence++
  catalogController?.abort()
})
</script>

<template>
  <section class="na-audience" aria-label="推送受众预估">
    <div class="na-audience__head">
      <h3>推送受众预估</h3>
      <button class="na-btn na-btn--quiet" type="button" :disabled="catalogPending" @click="loadCatalog">
        {{ catalogPending ? '正在加载主题…' : catalog ? '刷新主题目录' : '选择受众主题' }}
      </button>
    </div>
    <p class="na-muted">按新闻主题查看去重后的受众范围，供运营人工判断；此处仅预估，不发送推送。</p>
    <p v-if="catalogError" class="na-message na-message--error" role="alert">{{ catalogError }}</p>
    <template v-if="catalog">
      <p class="na-audience__source" role="status">
        <span class="na-badge" :class="{ 'na-badge--sample': catalog.source_mode === 'synthetic' }">
          {{ catalog.source_mode === 'synthetic' ? '模拟目录与受众' : '企业主题目录' }}
        </span>
        <span>{{ catalog.status === 'draft' ? '草案目录' : '已批准目录' }}</span>
      </p>
      <p v-if="!groups.length" class="na-muted">目录中暂无可选主题。</p>
      <div v-else class="na-audience__topics">
        <p class="na-muted">选择主题或细分主题，纳入和排除各最多10项；排除范围优先，人数以服务端去重预估为准。</p>
        <fieldset v-for="group in groups" :key="group.root.topic_id" class="na-audience__group">
          <legend>{{ group.root.name }}</legend>
          <div
            v-for="topic in [group.root, ...group.children]" :key="topic.topic_id"
            class="na-audience__topic" :class="{ 'na-audience__topic--child': topic.parent_topic_id }"
          >
            <span>{{ topic.parent_topic_id ? '↳ ' : '' }}{{ topic.name }}<small v-if="!topic.enabled || !group.root.enabled">（未开放）</small></span>
            <label><input type="checkbox" :checked="includeTopicIds.includes(topic.topic_id)" :disabled="!topic.enabled || !group.root.enabled" @change="toggleTopic(topic.topic_id, 'include')"> 纳入<span class="na-audience__sr">{{ topic.name }}</span></label>
            <label><input type="checkbox" :checked="excludeTopicIds.includes(topic.topic_id)" :disabled="!topic.enabled || !group.root.enabled" @change="toggleTopic(topic.topic_id, 'exclude')"> 排除<span class="na-audience__sr">{{ topic.name }}</span></label>
          </div>
        </fieldset>
      </div>
      <p v-if="selectionError" class="na-message na-message--error" role="alert">{{ selectionError }}</p>
      <div v-if="groups.length" class="na-audience__actions">
        <label>匹配方式
          <select v-model="match" class="na-select">
            <option value="any">任一所选主题</option>
            <option value="all">全部所选主题</option>
          </select>
        </label>
        <button class="na-btn na-btn--primary" type="button" :disabled="!canPreview" @click="estimateAudience">
          {{ previewPending ? '正在预估…' : '预估受众' }}
        </button>
        <span v-if="!includeTopicIds.length" class="na-muted">至少纳入一个主题</span>
      </div>
      <p v-if="previewError" class="na-message na-message--error" role="alert">{{ previewError }}</p>
      <section v-if="preview" class="na-audience__result" aria-label="受众预估结果" aria-live="polite">
        <p v-if="expired" class="na-notice" role="status">此预估已过期，请重新预估后再作运营判断。</p>
        <template v-else>
          <p class="na-audience__source"><span class="na-badge" :class="{ 'na-badge--sample': preview.snapshot.source_mode === 'synthetic' }">{{ preview.snapshot.source_mode === 'synthetic' ? '模拟受众人数' : '企业聚合预估' }}</span></p>
          <dl class="na-audience__counts">
            <div><dt>匹配受众（去重）</dt><dd>{{ preview.snapshot.matched_users === null ? '未提供' : formatDecimal(preview.snapshot.matched_users, 0) }}</dd></div>
            <div><dt>符合通知资格（去重快照）</dt><dd>{{ preview.snapshot.eligible_users === null ? '未提供' : formatDecimal(preview.snapshot.eligible_users, 0) }}</dd></div>
          </dl>
          <p><strong>{{ preview.recommendation.action === 'consider' ? '可考虑提交人工评估' : '建议继续观察' }}</strong>：{{ preview.recommendation.reason }}</p>
          <p class="na-muted">预估不代表发送承诺或审批通过，实际触达仍需运营审核与推送平台确认。</p>
        </template>
        <p class="na-muted">生成于 {{ formatDateTime(preview.snapshot.generated_at) }} · 有效至 {{ formatDateTime(preview.snapshot.expires_at) }}</p>
        <ul v-if="preview.limitations.length" class="na-audience__limitations"><li v-for="limitation in preview.limitations" :key="limitation">{{ limitation }}</li></ul>
        <details class="na-disclosure">
          <summary>预估依据与版本</summary>
          <dl class="na-defs">
            <div><dt>主题目录</dt><dd>{{ preview.selection.taxonomy_version }}</dd></div>
            <div><dt>画像版本</dt><dd>{{ preview.snapshot.profile_version }}</dd></div>
            <div><dt>数据版本</dt><dd>{{ preview.snapshot.source_version }}</dd></div>
            <div><dt>数据水位</dt><dd>{{ formatDateTime(preview.snapshot.watermark) }}</dd></div>
            <div><dt>统计范围</dt><dd>{{ formatDateTime(preview.snapshot.window_start) }} ～ {{ formatDateTime(preview.snapshot.window_end) }}</dd></div>
            <div><dt>预估引用</dt><dd>{{ preview.snapshot.snapshot_id }}</dd></div>
          </dl>
        </details>
      </section>
    </template>
  </section>
</template>

<style scoped>
.na-audience { margin-top: 18px; padding: 16px; background: var(--na-bg, #fff); border: 1px solid var(--na-border, #ddd); border-radius: 10px; }
.na-audience__head, .na-audience__source, .na-audience__actions { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
.na-audience__head { justify-content: space-between; }
.na-audience h3 { margin: 0; font-size: 15px; }
.na-audience p, .na-audience li { font-size: 12px; line-height: 1.7; }
.na-audience__group { margin: 10px 0; padding: 8px 12px; border: 1px solid var(--na-border, #ddd); border-radius: 7px; }
.na-audience__group legend { font-size: 13px; font-weight: 600; }
.na-audience__topic { display: grid; grid-template-columns: minmax(100px, 1fr) auto auto; align-items: center; gap: 18px; padding: 6px 0; font-size: 13px; }
.na-audience__topic--child > span { padding-left: 12px; }
.na-audience__topic small { color: var(--na-text-muted); }
.na-audience__topic label { display: flex; gap: 4px; align-items: center; cursor: pointer; }
.na-audience__actions { margin-top: 12px; }
.na-audience__actions > label { display: flex; gap: 8px; align-items: center; font-size: 12px; }
.na-audience__result { margin-top: 14px; border-top: 1px solid var(--na-border, #ddd); padding-top: 10px; }
.na-audience__counts { display: flex; flex-wrap: wrap; gap: 24px; }
.na-audience__counts dt { color: var(--na-text-muted); font-size: 12px; }
.na-audience__counts dd { margin: 4px 0 0; font-size: 22px; font-weight: 600; }
.na-audience__limitations { padding-left: 18px; color: var(--na-text-muted); }
.na-audience dd { overflow-wrap: anywhere; }
.na-audience__sr { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; }
@media (max-width: 600px) { .na-audience { padding: 12px; } .na-audience__topic { gap: 10px; } }
</style>
