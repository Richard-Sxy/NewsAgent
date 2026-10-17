<script setup lang="ts">
import { ref } from 'vue'

import type { HotNewsRankedItemView } from '@/api'
import NaJsonBlock from '@/components/NaJsonBlock.vue'
import AudiencePreviewPanel from '@/components/hotnews/AudiencePreviewPanel.vue'
import NewsReaderProfilePanel from '@/components/hotnews/NewsReaderProfilePanel.vue'
import { formatDecimal, formatRatioPercent } from '@/utils/format'

defineProps<{
  items: HotNewsRankedItemView[]
  runId?: string
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
        <th style="width: 48px">排名</th>
        <th style="min-width: 240px">新闻</th>
        <th style="width: 120px">阅读表现</th>
        <th style="width: 90px">热度</th>
        <th style="width: 90px">置信度</th>
        <th style="width: 160px">操作</th>
      </tr>
    </thead>
    <tbody>
      <template v-for="item in items" :key="item.news_id">
        <tr>
          <td><span class="na-hot-rank__position" :class="{ 'is-leading': item.rank <= 3 }">{{ item.rank }}</span></td>
          <td>
            <div class="na-cell__title" :title="item.title ?? undefined">
              {{ item.title || '未获取新闻标题' }}
            </div>
            <div class="na-hot-rank__meta">
              <span>{{ contentTypeText(item.metrics.content_type) }}</span>
              <span class="na-cell__sub na-cell__sub--compact" :title="item.news_id">{{ compactNewsId(item.news_id) }}</span>
            </div>
          </td>
          <td>
            <span>{{ formatDecimal(item.metrics.clicks) }} 次点击</span>
            <small class="na-hot-rank__secondary" :title="item.metrics.ctr">CTR {{ formatRatioPercent(item.metrics.ctr) }}</small>
          </td>
          <td class="na-hot-rank__score" :title="item.hot_score.score">{{ formatDecimal(item.hot_score.score, 4) }}</td>
          <td>
            <span v-if="item.analysis">{{ formatRatioPercent(item.analysis.overall_confidence) }}</span>
            <span v-else class="na-muted">—</span>
          </td>
          <td>
            <div class="na-hot-rank__actions">
              <button
                class="na-btn na-btn--quiet"
                type="button"
                :aria-expanded="expanded.has(item.news_id)"
                @click="toggle(item.news_id)"
              >
                {{ expanded.has(item.news_id) ? '收起' : item.analysis ? '分析' : '详情' }}
              </button>
              <button class="na-btn" type="button" @click="emit('decide', item)">决策</button>
            </div>
          </td>
        </tr>
        <tr v-if="expanded.has(item.news_id)" class="na-hot-rank__detail">
          <td colspan="6">
            <details v-if="item.content_excerpt" class="na-disclosure">
              <summary>用于本次分析的正文片段</summary>
              <p class="na-hot-rank__excerpt">{{ item.content_excerpt }}</p>
            </details>
            <template v-if="item.analysis">
              <p><strong>趋势判断：</strong>{{ item.analysis.trend_assessment }}</p>
              <ul v-if="item.analysis.operation_suggestions.length" class="na-hot-rank__suggestions" aria-label="运营建议">
                <li v-for="(suggestion, index) in item.analysis.operation_suggestions" :key="index">
                  <p>{{ typeof suggestion.action === 'string' ? suggestion.action : '完整建议见下方分析详情。' }}</p>
                  <small v-if="typeof suggestion.rationale === 'string'" class="na-muted">{{ suggestion.rationale }}</small>
                </li>
              </ul>
              <p v-if="item.analysis.limitations.length" class="na-muted">
                限制：{{ item.analysis.limitations.join('；') }}
              </p>
            </template>
            <NewsReaderProfilePanel v-if="item.reader_profile_summary" :summary="item.reader_profile_summary" :trace="item.reader_profile_trace" />
            <AudiencePreviewPanel v-if="runId && item.analysis" :key="`${runId}:${item.news_id}`" :run-id="runId" :news-id="item.news_id" />
            <details v-if="item.retrieval_trace" class="na-disclosure">
              <summary>主题兴趣与相关新闻排序依据（本地合成画像）</summary>
              <p class="na-muted">兴趣权重用于相关新闻排序；热点分数仍按原指标计算。</p>
              <NaJsonBlock :value="item.retrieval_trace" label="主题、画像时点与候选排序证据" />
            </details>
            <details class="na-disclosure">
              <summary>指标与完整分析详情</summary>
              <dl class="na-defs">
                <div><dt>曝光</dt><dd>{{ formatDecimal(item.metrics.impressions) }}</dd></div>
                <div><dt>点击分量</dt><dd>{{ formatDecimal(item.hot_score.click_component, 4) }}</dd></div>
                <div><dt>消费分量</dt><dd>{{ formatDecimal(item.hot_score.consumption_component, 4) }}</dd></div>
                <div><dt>互动分量</dt><dd>{{ formatDecimal(item.hot_score.interaction_component, 4) }}</dd></div>
                <div><dt>增长分量</dt><dd>{{ formatDecimal(item.hot_score.growth_component, 4) }}</dd></div>
              </dl>
              <template v-if="item.analysis">
                <p class="na-muted">
                  主导驱动：{{ item.analysis.dominant_driver }} · 证据：{{ item.analysis.evidence_news_ids.join(', ') || '无' }}
                  · 模型请求：{{ item.analysis.model_request_id ?? '-' }}
                </p>
                <NaJsonBlock v-if="item.analysis.operation_suggestions.length" :value="item.analysis.operation_suggestions" label="完整运营建议" />
              </template>
            </details>
          </td>
        </tr>
      </template>
    </tbody>
  </table>
</template>

<style scoped>
.na-hot-rank__position { display: inline-flex; align-items: center; justify-content: center; width: 26px; height: 26px; border-radius: 7px; color: var(--na-text-muted); font-size: 12px; }
.na-hot-rank__position.is-leading { background: var(--na-accent-weak); color: var(--na-accent); font-weight: 600; }
.na-hot-rank__meta { display: flex; align-items: center; gap: 10px; margin-top: 5px; font-size: 11px; color: var(--na-text-muted); }
.na-hot-rank__secondary { display: block; margin-top: 3px; color: var(--na-text-muted); font-size: 11px; }
.na-hot-rank__score { font-variant-numeric: tabular-nums; font-weight: 600; color: var(--na-accent); }
.na-hot-rank__actions { display: flex; gap: 6px; white-space: nowrap; }
.na-hot-rank__detail { background: #fafcf9; }
.na-hot-rank__detail td { padding: 12px 18px 18px; }
.na-hot-rank__suggestions { display: grid; gap: 8px; padding-left: 20px; }
.na-hot-rank__suggestions p { margin: 0; }
.na-hot-rank__excerpt { white-space: pre-wrap; line-height: 1.8; }
</style>
