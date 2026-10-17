import { request } from '@/api/http'

export type AudienceSourceMode = 'synthetic' | 'enterprise'

export interface AudienceTopic {
  topic_id: string
  name: string
  parent_topic_id: string | null
  enabled: boolean
  provenance: unknown
}

export interface AudienceTopicCatalog {
  schema_version: 1
  taxonomy_version: string
  status: 'draft' | 'approved'
  topics: AudienceTopic[]
  source_mode: AudienceSourceMode
}

export interface AudienceSelection {
  taxonomy_version: string
  include_topic_ids: string[]
  exclude_topic_ids: string[]
  match: 'any' | 'all'
}

export interface AudienceSnapshot {
  source_mode: AudienceSourceMode
  run_id: string
  news_id: string
  matched_users: number | null
  eligible_users: number | null
  snapshot_id: string
  profile_version: string
  source_version: string
  generated_at: string
  expires_at: string
  watermark: string
  window_start: string
  window_end: string
}

export interface AudiencePreview {
  preview_only: true
  manual_review_required: true
  dispatch_enabled: false
  run_id: string
  news_id: string
  selection: AudienceSelection
  snapshot: AudienceSnapshot
  recommendation: { action: 'consider' | 'observe'; reason: string }
  fingerprint: string
  limitations: string[]
}

export class AudienceResponseError extends Error {
  constructor() {
    super('受众服务返回的数据无法核对，请重新加载。')
    this.name = 'AudienceResponseError'
  }
}

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new AudienceResponseError()
  return value as Record<string, unknown>
}

function text(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0 && value.length <= 2000
}

function ids(value: unknown): value is string[] {
  return Array.isArray(value) && value.length <= 100 && value.every(text) && new Set(value).size === value.length
}

function sourceMode(value: unknown): value is AudienceSourceMode {
  return value === 'synthetic' || value === 'enterprise'
}

export function sameAudienceSelection(left: AudienceSelection, right: AudienceSelection): boolean {
  const sameIds = (first: string[], second: string[]) =>
    JSON.stringify([...first].sort()) === JSON.stringify([...second].sort())
  return left.taxonomy_version === right.taxonomy_version && left.match === right.match
    && sameIds(left.include_topic_ids, right.include_topic_ids)
    && sameIds(left.exclude_topic_ids, right.exclude_topic_ids)
}

/** Display only a coherent two-level directory; the backend remains the authority. */
export function parseAudienceTopics(value: unknown): AudienceTopicCatalog {
  const catalog = record(value)
  if (catalog.schema_version !== 1 || !text(catalog.taxonomy_version)
      || !['draft', 'approved'].includes(String(catalog.status)) || !sourceMode(catalog.source_mode)
      || !Array.isArray(catalog.topics) || catalog.topics.length > 1000) throw new AudienceResponseError()
  const topics = catalog.topics.map((value) => {
    const topic = record(value)
    if (!text(topic.topic_id) || !text(topic.name) || typeof topic.enabled !== 'boolean'
        || (topic.parent_topic_id !== null && !text(topic.parent_topic_id))) throw new AudienceResponseError()
    return topic as unknown as AudienceTopic
  })
  const byId = new Map(topics.map((topic) => [topic.topic_id, topic]))
  if (byId.size !== topics.length || topics.some((topic) => topic.parent_topic_id !== null
    && (!byId.has(topic.parent_topic_id) || byId.get(topic.parent_topic_id)?.parent_topic_id !== null))) {
    throw new AudienceResponseError()
  }
  return { ...catalog, topics } as unknown as AudienceTopicCatalog
}

/** Bind the displayed estimate to this exact news item and selection. */
export function parseAudiencePreview(value: unknown, runId: string, newsId: string,
                                     expected: AudienceSelection): AudiencePreview {
  const preview = record(value)
  const selection = record(preview.selection)
  const snapshot = record(preview.snapshot)
  const recommendation = record(preview.recommendation)
  const validCount = (count: unknown) => count === null || (Number.isSafeInteger(count) && Number(count) >= 0)
  if (preview.preview_only !== true || preview.manual_review_required !== true || preview.dispatch_enabled !== false
      || preview.run_id !== runId || preview.news_id !== newsId
      || snapshot.run_id !== runId || snapshot.news_id !== newsId
      || !text(selection.taxonomy_version) || !ids(selection.include_topic_ids) || !ids(selection.exclude_topic_ids)
      || !['any', 'all'].includes(String(selection.match))
      || !sameAudienceSelection(selection as unknown as AudienceSelection, expected)
      || !sourceMode(snapshot.source_mode) || !validCount(snapshot.matched_users) || !validCount(snapshot.eligible_users)
      || (snapshot.matched_users !== null && snapshot.eligible_users !== null
          && Number(snapshot.eligible_users) > Number(snapshot.matched_users))
      || !['snapshot_id', 'profile_version', 'source_version'].every((key) => text(snapshot[key]))
      || !['generated_at', 'expires_at', 'watermark', 'window_start', 'window_end'].every((key) =>
        text(snapshot[key]) && Number.isFinite(Date.parse(snapshot[key] as string)))
      || Date.parse(snapshot.generated_at as string) >= Date.parse(snapshot.expires_at as string)
      || Date.parse(snapshot.window_start as string) >= Date.parse(snapshot.window_end as string)
      || !['consider', 'observe'].includes(String(recommendation.action)) || !text(recommendation.reason)
      || !text(preview.fingerprint) || !Array.isArray(preview.limitations) || !preview.limitations.every(text)) {
    throw new AudienceResponseError()
  }
  return preview as unknown as AudiencePreview
}

const BASE = '/api/v1/audience'

export const audienceApi = {
  async topics(signal?: AbortSignal): Promise<AudienceTopicCatalog> {
    return parseAudienceTopics(await request<unknown>(`${BASE}/topics`, { signal }))
  },
  async preview(runId: string, newsId: string, selection: AudienceSelection,
                signal?: AbortSignal): Promise<AudiencePreview> {
    const value = await request<unknown>(`${BASE}/runs/${encodeURIComponent(runId)}/news/${encodeURIComponent(newsId)}/preview`, {
      method: 'POST', body: selection, signal,
    })
    return parseAudiencePreview(value, runId, newsId, selection)
  },
}
