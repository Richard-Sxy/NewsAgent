<script setup lang="ts">
import { ref } from 'vue'

import type { HotNewsRankedItemView } from '@/api'
import NaJsonBlock from '@/components/NaJsonBlock.vue'
import { formatDecimal, formatRatioPercent } from '@/utils/format'

defineProps<{
  items: HotNewsRankedItemView[]
}>()

const emit = defineEmits<{
  decide: [item: HotNewsRankedItemView]
}>()

/** 展开分析摘要的行（news_id 集合）。 */
const expanded = ref<Set<string>>(new Set())

function toggle(newsId: string): void {
  const next = new Set(expanded.value)
  if (next.has(newsId)) {
    next.delete(newsId)
  } else {
    next.add(newsId)
  }
  expanded.value = next
}

function compactNewsId(value: string): string {
  if (value.length <= 14) return value
  return `${value.slice(0, 8)}…${value.slice(-5)}`
}

function contentTypeText(value: string): string {
  const labels: Record<string, string> = {
    article: '图文',
    video: '视频',
  }
  return labels[value] ?? value
}
</script>

<template>
  <table class="na-table">
    <thead>
      <tr>
        <th style="width: 48px">#</th>
        <th style="width: 72px">类型</th>
        <th style="min-width: 220px">新闻</th>
        <th style="width: 84px">曝光</th>
        <th style="width: 72px">点击</th>
        <th style="width: 72px">CTR</th>
        <th style="width: 200px">热度（点击/消费/互动/增长）</th>
        <th style="width: 72px">置信度</th>
        <th style="width: 150px">操作</th>
      </tr>
    </thead>
    <tbody>
      <template v-for="item in items" :key="item.news_id">
        <tr>
          <td>{{ item.rank }}</td>
          <td>
            <span class="na-badge na-badge--neutral">
              {{ contentTypeText(item.metrics.content_type) }}
            </span>
          </td>
          <td>
            <div class="na-cell__title" :title="item.title ?? undefined">
              {{ item.title || '未获取新闻标题' }}
            </div>
            <div class="na-cell__sub na-cell__sub--compact" :title="item.news_id">
              {{ compactNewsId(item.news_id) }}
            </div>
          </td>
          <td>{{ formatDecimal(item.metrics.impressions) }}</td>
          <td>{{ formatDecimal(item.metrics.clicks) }}</td>
          <td class="na-mono" :title="item.metrics.ctr">{{ formatRatioPercent(item.metrics.ctr) }}</td>
          <td class="na-mono">
            <span :title="item.hot_score.score">{{ formatDecimal(item.hot_score.score, 4) }}</span>
            <span class="na-muted">
              ({{ formatDecimal(item.hot_score.click_component, 4) }}/{{ formatDecimal(item.hot_score.consumption_component, 4) }}/{{
                formatDecimal(item.hot_score.interaction_component, 4)
              }}/{{ formatDecimal(item.hot_score.growth_component, 4) }})
            </span>
          </td>
          <td>
            <span v-if="item.analysis">{{ formatRatioPercent(item.analysis.overall_confidence) }}</span>
            <span v-else class="na-muted">—</span>
          </td>
          <td>
            <button
              v-if="item.analysis"
              class="na-btn"
              type="button"
              @click="toggle(item.news_id)"
            >
              {{ expanded.has(item.news_id) ? '收起' : '分析' }}
            </button>
            <button class="na-btn" type="button" @click="emit('decide', item)">决策</button>
          </td>
        </tr>
        <tr v-if="item.analysis && expanded.has(item.news_id)">
          <td colspan="9">
            <p><strong>趋势判断：</strong>{{ item.analysis.trend_assessment }}</p>
            <p class="na-muted">
              主导驱动：{{ item.analysis.dominant_driver }} · 证据：{{
                item.analysis.evidence_news_ids.join(', ') || '无'
              }}
              · 模型请求：{{ item.analysis.model_request_id ?? '-' }}
            </p>
            <p v-if="item.analysis.limitations.length" class="na-muted">
              限制：{{ item.analysis.limitations.join('；') }}
            </p>
            <NaJsonBlock
              v-if="item.analysis.operation_suggestions.length"
              :value="item.analysis.operation_suggestions"
              label="运营建议"
            />
          </td>
        </tr>
      </template>
    </tbody>
  </table>
</template>
