import type { ConversationToolTrace } from '@/api/conversations'

type ObjectValue = Record<string, unknown>

export interface RelatedArticle {
  newsId: string
  chunkId: string | null
  title: string
  excerpt: string
  sourceUrl: string | null
  publishTime: string | null
  contentVersion: string | null
  embeddingVersion: string | null
}

export interface RelatedArticleGroup {
  key: string
  sourceNewsId: string | null
  sourceTitle: string
  sourceRunId: string | null
  query: string | null
  status: 'completed' | 'failed'
  items: RelatedArticle[]
  notices: string[]
}

const MAX_SOURCES = 3
const MAX_ARTICLES = 3
export const RELATED_EXCERPT_PREVIEW_CHARS = 300
export const RELATED_EXCERPT_MAX_CHARS = 600

const object = (value: unknown): ObjectValue | null =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? value as ObjectValue : null
const text = (value: unknown, max: number): string =>
  typeof value === 'string' ? Array.from(value.trim()).slice(0, max).join('') : ''
const identifier = (value: unknown): string | null =>
  typeof value === 'string' && value.trim() && value.length <= 256 ? value.trim() : null

/** 原文链接仅允许完整 HTTP(S) URL；拒绝凭证和控制字符，不修复非法协议。 */
export function safeRelatedArticleUrl(value: unknown): string | null {
  if (typeof value !== 'string' || value.length > 2048 || Array.from(value).some((char) => {
    const point = char.codePointAt(0)!
    return point < 32 || (point >= 127 && point <= 159)
  })) return null
  const raw = value.trim()
  if (!/^https?:\/\//i.test(raw)) return null
  try {
    const url = new URL(raw)
    return ['http:', 'https:'].includes(url.protocol) && url.hostname && !url.username && !url.password
      ? url.href : null
  } catch {
    return null
  }
}

function publicationTime(value: unknown): string | null {
  if (typeof value !== 'string' || value.length > 64) return null
  const parts = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/i.exec(value)
  if (!parts || Number.isNaN(Date.parse(value))) return null
  const year = Number(parts[1]), month = Number(parts[2]), day = Number(parts[3])
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)
  const days = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
  if (month < 1 || month > 12 || day < 1 || day > days[month - 1]!
    || Number(parts[4]) > 23 || Number(parts[5]) > 59 || Number(parts[6]) > 59) return null
  return value
}

function article(value: unknown, sourceNewsId: string | null): RelatedArticle | null {
  const item = object(value)
  const newsId = identifier(item?.news_id)
  if (!item || !newsId || newsId === sourceNewsId) return null
  return {
    newsId,
    chunkId: identifier(item.chunk_id),
    title: text(item.title, 200) || `新闻 ${newsId}`,
    excerpt: text(item.excerpt, RELATED_EXCERPT_MAX_CHARS),
    sourceUrl: safeRelatedArticleUrl(item.source_url),
    publishTime: publicationTime(item.publish_time),
    contentVersion: identifier(item.content_version),
    embeddingVersion: identifier(item.embedding_version),
  }
}

/** 仅投影已保存工具结果；旧历史无此字段时不生成空卡片，也不解析模型文本。 */
export function projectRelatedArticles(tools: readonly ConversationToolTrace[]): RelatedArticleGroup[] {
  if (!Array.isArray(tools)) return []
  const groups = new Map<string, RelatedArticleGroup>()

  function merge(incoming: RelatedArticleGroup, items: unknown): void {
    const existing = groups.get(incoming.key)
    if (!existing && groups.size >= MAX_SOURCES) return
    if (existing?.status === 'completed' && incoming.status === 'failed') return
    const group = existing ?? incoming
    if (existing?.status === 'failed' && incoming.status === 'completed') group.notices = []
    if (incoming.status === 'completed') group.status = 'completed'
    if (group.sourceTitle === `新闻 ${group.sourceNewsId}` && incoming.sourceTitle) group.sourceTitle = incoming.sourceTitle
    group.sourceRunId ??= incoming.sourceRunId
    group.query ??= incoming.query
    group.notices = [...new Set([...group.notices, ...incoming.notices])].slice(0, 3)
    if (incoming.status === 'completed' && Array.isArray(items)) {
      const seen = new Set(group.items.map((item) => item.newsId))
      for (const raw of items) {
        if (group.items.length >= MAX_ARTICLES) break
        const item = article(raw, group.sourceNewsId)
        if (!item || seen.has(item.newsId)) continue
        group.items.push(item)
        seen.add(item.newsId)
      }
    }
    groups.set(group.key, group)
  }

  for (const value of tools) {
    const trace = object(value)
    if (!trace || trace.status !== 'completed') continue
    const result = object(trace.result)
    if (!result) continue
    if (trace.name === 'read_hot_news' || trace.name === 'query_hot_news') {
      const runId = identifier(result.run_id)
      if (!runId || !Array.isArray(result.items) || !Array.isArray(result.related_articles)) continue
      const sourceIds = new Set(result.items.map((item) => identifier(object(item)?.news_id)).filter(Boolean))
      for (const raw of result.related_articles) {
        const source = object(raw)
        const newsId = identifier(source?.source_news_id)
        if (!source || !newsId || !sourceIds.has(newsId) || source.source_run_id !== runId
          || (source.status !== 'completed' && source.status !== 'failed')
          || (source.status === 'completed' && !Array.isArray(source.items))) continue
        const notice = text(source.notice, 400)
        merge({
          key: `news:${newsId}`, sourceNewsId: newsId,
          sourceTitle: text(source.source_title, 200) || `新闻 ${newsId}`,
          sourceRunId: runId, query: null, status: source.status,
          items: [], notices: notice ? [notice] : [],
        }, source.items)
      }
    } else if (trace.name === 'search_knowledge') {
      const query = text(result.query, 500)
      const newsId = identifier(result.source_news_id)
      if (!query || !Array.isArray(result.items)) continue
      const notice = text(result.coverage_note, 400)
      merge({
        key: newsId ? `news:${newsId}` : `query:${query}`,
        sourceNewsId: newsId, sourceTitle: query,
        sourceRunId: null, query, status: 'completed', items: [], notices: notice ? [notice] : [],
      }, result.items)
    }
  }
  return [...groups.values()]
}

export function relatedExcerptPreview(excerpt: string, expanded = false): string {
  const chars = Array.from(excerpt)
  const limit = expanded ? RELATED_EXCERPT_MAX_CHARS : RELATED_EXCERPT_PREVIEW_CHARS
  return chars.length > limit ? `${chars.slice(0, limit).join('')}…` : excerpt
}

export function relatedExcerptExpandable(excerpt: string): boolean {
  return Array.from(excerpt).length > RELATED_EXCERPT_PREVIEW_CHARS
}
