<script setup lang="ts">
import { computed } from 'vue'

import type { NewsReaderProfileSummary } from '@/api'
import NaJsonBlock from '@/components/NaJsonBlock.vue'
import { formatDateTime, formatDecimal, formatRatioPercent } from '@/utils/format'

const props = defineProps<{
  summary: NewsReaderProfileSummary
  trace?: Record<string, unknown> | null
}>()

const interests = computed(() => [
  { name: '长期兴趣', value: props.summary.long_term },
  { name: '短期兴趣', value: props.summary.short_term },
])

function topicName(topicId: string): string {
  const names = props.trace?.topic_names
  const value = names && typeof names === 'object' ? (names as Record<string, unknown>)[topicId] : null
  return typeof value === 'string' && value.length <= 100 ? value : topicId
}
</script>

<template>
  <section class="na-reader-profile" aria-label="该新闻访问人群画像">
    <h4>该新闻访问人群 · {{ summary.source_mode === 'synthetic' ? '合成样例' : '企业聚合' }}</h4>
    <p>
      本窗口去重读者 {{ formatDecimal(summary.reader_count) }} 人，画像截至 {{ formatDateTime(summary.profile_cutoff) }}。
      <span v-if="summary.trigger_article_excluded">兴趣计算已排除当前文章。</span>
    </p>
    <p v-if="summary.source_mode === 'synthetic'" class="na-muted">合成行为用于演示分析链路，不能代表真实受众规模或推送效果。</p>
    <p v-if="summary.status === 'empty'" class="na-muted">该新闻在已覆盖的窗口中暂无{{ summary.source_mode === 'synthetic' ? '模拟' : '' }}读者，无法据此提出受众方向。</p>
    <div v-else class="na-reader-profile__grid">
      <div v-for="interest in interests" :key="interest.name">
        <strong>{{ interest.name }} · 回看 {{ interest.value.lookback_hours }} 小时</strong>
        <p class="na-muted">
          有画像 {{ formatDecimal(interest.value.profiled_reader_count) }} 人；未知 {{ formatDecimal(interest.value.unknown_reader_count) }} 人
          （含冷启动 {{ formatDecimal(interest.value.cold_start_reader_count) }} 人）。
        </p>
        <table v-if="interest.value.topics.length" class="na-table">
          <thead><tr><th>兴趣主题</th><th>读者</th><th>全体读者占比</th><th>相对基线</th></tr></thead>
          <tbody>
            <tr v-for="topic in interest.value.topics.slice(0, 5)" :key="topic.topic_id">
              <td :title="topic.topic_id">{{ topicName(topic.topic_id) }}</td>
              <td>{{ formatDecimal(topic.reader_count) }}</td>
              <td>{{ formatRatioPercent(topic.share_all) }}</td>
              <td>{{ topic.lift === null ? (topic.low_sample ? '样本不足' : '无可比基线') : `${formatDecimal(topic.lift, 2)} 倍` }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="na-muted">没有达到当前兴趣阈值的主题。</p>
      </div>
    </div>
    <p class="na-muted">一人可以有多个主题，占比不可相加；主题倾向供人工判断，可触达人数需另行核验。</p>
    <details class="na-disclosure">
      <summary>画像口径与聚合证据</summary>
      <p v-if="summary.error_bound !== null">企业近似去重误差上界：{{ formatRatioPercent(summary.error_bound) }}</p>
      <p v-if="summary.limitations.length">{{ summary.limitations.join('；') }}</p>
      <NaJsonBlock :value="{ summary, trace }" label="本新闻读者聚合快照" />
    </details>
  </section>
</template>

<style scoped>
.na-reader-profile { border: 1px solid var(--na-border, #dce5dd); border-radius: 8px; padding: 12px; margin: 12px 0; background: white; }
.na-reader-profile h4 { margin: 0 0 8px; }
.na-reader-profile__grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }
.na-reader-profile p { line-height: 1.7; }
.na-reader-profile th { white-space: normal; }
</style>
