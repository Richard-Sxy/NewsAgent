<script setup lang="ts">
import { computed, ref } from 'vue'

import type { ProgressEvent } from '@/api'
import { formatDateTime } from '@/utils/format'
import { eventLabel, eventTone, stepLabel } from '@/utils/labels'

const props = defineProps<{
  events: ProgressEvent[]
  streamState: string
  streamError: string | null
}>()

const keyword = ref('')
const eventFilter = ref('')

const eventNames = computed(() => {
  const seen = new Set<string>()
  for (const item of props.events) seen.add(item.event)
  return [...seen]
})

const visible = computed(() => {
  const text = keyword.value.trim().toLowerCase()
  return props.events.filter((item) => {
    if (eventFilter.value && item.event !== eventFilter.value) return false
    if (!text) return true
    const haystack = [item.message ?? '', item.step ?? '', item.section_id ?? '', item.event]
      .join(' ')
      .toLowerCase()
    return haystack.includes(text)
  })
})

/** 最新事件排在最上面，运营不必往下翻。 */
const newestFirst = computed(() => [...visible.value].reverse())

function reset(): void {
  keyword.value = ''
  eventFilter.value = ''
}
</script>

<template>
  <div class="na-stream">
    <div class="na-row">
      <div class="na-field na-field--grow">
        <label for="event-keyword">关键词</label>
        <input
          id="event-keyword"
          v-model="keyword"
          class="na-input"
          placeholder="按消息、步骤或章节筛选"
          autocomplete="off"
        >
      </div>
      <div class="na-field na-field--fixed">
        <label for="event-name">事件类型</label>
        <select id="event-name" v-model="eventFilter" class="na-select">
          <option value="">全部事件</option>
          <option v-for="name in eventNames" :key="name" :value="name">{{ eventLabel(name) }}</option>
        </select>
      </div>
      <div class="na-field na-field--action">
        <button class="na-btn" type="button" :disabled="!keyword && !eventFilter" @click="reset">
          清空筛选
        </button>
      </div>
    </div>

    <p class="na-muted">
      已接收 {{ events.length }} 个事件，当前显示 {{ visible.length }} 个 · 连接状态：{{ streamState }}
    </p>
    <p v-if="streamError" class="na-message na-message--error">{{ streamError }}</p>

    <ol v-if="newestFirst.length" class="na-stream__list">
      <li
        v-for="(item, index) in newestFirst"
        :key="`${item.event_id ?? index}-${item.occurred_at}`"
        class="na-stream__item"
      >
        <div class="na-stream__head">
          <span class="na-badge" :class="`na-badge--${eventTone(item.event)}`">
            {{ eventLabel(item.event) }}
          </span>
          <span class="na-muted">
            {{ stepLabel(item.step) }} · {{ item.progress.percent }}%（{{ item.progress.completed }}/{{
              item.progress.total
            }}）
          </span>
          <span class="na-muted na-stream__time">{{ formatDateTime(item.occurred_at) }}</span>
        </div>
        <p v-if="item.message" class="na-stream__message">{{ item.message }}</p>
        <details class="na-stream__raw">
          <summary>原始载荷</summary>
          <pre class="na-log">{{ JSON.stringify(item, null, 2) }}</pre>
        </details>
      </li>
    </ol>
    <p v-else class="na-empty">暂无事件（任务开始执行后会自动推送）</p>
  </div>
</template>
