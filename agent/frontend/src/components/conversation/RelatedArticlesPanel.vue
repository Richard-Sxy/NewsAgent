<script setup lang="ts">
import { computed, reactive } from 'vue'
import type { ConversationToolTrace } from '@/api/conversations'
import { formatDateTime } from '@/utils/format'
import { projectRelatedArticles, relatedExcerptExpandable, relatedExcerptPreview } from '@/utils/relatedArticles'

const props = defineProps<{ tools: ConversationToolTrace[] }>()
const groups = computed(() => projectRelatedArticles(props.tools))
const expanded = reactive(new Set<string>())
const articleKey = (groupKey: string, newsId: string): string => JSON.stringify([groupKey, newsId])

function toggleExcerpt(key: string): void {
  if (expanded.has(key)) expanded.delete(key)
  else expanded.add(key)
}
</script>

<template>
  <section v-if="groups.length" class="na-related-articles" aria-label="相关新闻检索结果">
    <h3>相关新闻</h3>
    <p class="na-muted na-related-articles__evidence">
      以下为检索到的新闻片段，供核对与延伸阅读；片段不代表完整原文，事实和时效请核对来源。
    </p>
    <section v-for="group in groups" :key="group.key" class="na-related-articles__group">
      <h4>{{ group.sourceNewsId ? `根据「${group.sourceTitle}」召回` : `检索「${group.query}」` }}</h4>
      <p v-if="group.status === 'failed'" class="na-muted" role="status">
        {{ group.notices[0] || '相关文章暂时无法检索，请稍后重试。' }}
      </p>
      <template v-else>
        <p v-if="!group.items.length" class="na-muted" role="status">未检索到相关新闻。</p>
        <ol v-else class="na-related-articles__list">
          <li v-for="item in group.items" :key="item.newsId" class="na-related-articles__article">
            <h5>{{ item.title }}</h5>
            <p class="na-muted na-related-articles__meta">
              发布时间：{{ item.publishTime ? formatDateTime(item.publishTime) : '未知' }}
              <a v-if="item.sourceUrl" :href="item.sourceUrl" target="_blank" rel="noopener noreferrer">查看原文</a>
              <span v-else>来源链接未提供</span>
            </p>
            <p class="na-related-articles__excerpt">{{ item.excerpt ? relatedExcerptPreview(item.excerpt, expanded.has(articleKey(group.key, item.newsId))) : '未提供内容片段。' }}</p>
            <button
              v-if="relatedExcerptExpandable(item.excerpt)"
              class="na-btn na-related-articles__expand"
              type="button"
              :aria-expanded="expanded.has(articleKey(group.key, item.newsId))"
              @click="toggleExcerpt(articleKey(group.key, item.newsId))"
            >
              {{ expanded.has(articleKey(group.key, item.newsId)) ? '收起片段' : '展开片段' }}
            </button>
          </li>
        </ol>
        <p v-for="notice in group.notices" :key="notice" class="na-muted na-related-articles__notice">{{ notice }}</p>
      </template>
    </section>
  </section>
</template>

<style scoped>
.na-related-articles { margin-top: 16px; padding: 16px; border: 1px solid var(--na-border, #ddd); border-radius: 10px; background: var(--na-bg, #fafafa); }
h3 { margin: 0 0 8px; font-size: 16px; }
h4 { margin: 0 0 10px; font-size: 14px; line-height: 1.6; overflow-wrap: anywhere; }
h5 { margin: 0; font-size: 14px; line-height: 1.6; overflow-wrap: anywhere; }
.na-related-articles__evidence { margin: 0 0 16px; font-size: 12px; line-height: 1.6; }
.na-related-articles__group + .na-related-articles__group { margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--na-border, #ddd); }
.na-related-articles__list { list-style: none; margin: 0; padding: 0; display: grid; gap: 12px; }
.na-related-articles__article { padding: 12px; border: 1px solid var(--na-border, #ddd); border-radius: 8px; }
.na-related-articles__meta { display: flex; flex-wrap: wrap; gap: 8px 16px; margin: 6px 0 8px; font-size: 12px; }
.na-related-articles__meta a { color: var(--na-accent); }
.na-related-articles__excerpt { margin: 0; font-size: 13px; line-height: 1.75; white-space: pre-wrap; overflow-wrap: anywhere; }
.na-related-articles__expand { margin-top: 8px; padding: 4px 8px; font-size: 12px; }
.na-related-articles__notice { margin: 10px 0 0; font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
</style>
