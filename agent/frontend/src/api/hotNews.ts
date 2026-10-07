import { request } from '@/api/http'
import type { SqlAssistantConfig, SqlAssistantPreviewRequest } from '@/api/sqlAssistant'
import type {
  HotNewsRunDetailResponse,
  HotNewsRunListResponse,
  LocalHotNewsRunResponse,
  RecordHotNewsDecisionRequest,
  RecordHotNewsDecisionResponse,
} from '@/api/types'

const BASE = '/api/v1/hot-news'

export interface ListHotNewsRunsQuery {
  offset?: number
  limit?: number
}

/**
 * 热点运营控制台接口。
 *
 * 该组端点与 Data Loop 复用同一个网关共享 Bearer Token（后端独立校验），
 * 但权限串来自独立的 `X-Hot-News-Roles` 角色头。前端**绝不**设置身份头：
 * 生产由企业网关注入，本地开发由 vite 代理层注入。
 */
export const hotNewsApi = {
  localSqlConfig(options: { signal?: AbortSignal } = {}) {
    return request<SqlAssistantConfig>('/api/v1/local-simulation/hot-news/sql-config', options)
  },

  runLocalSimulation(body?: SqlAssistantPreviewRequest) {
    return request<LocalHotNewsRunResponse>('/api/v1/local-simulation/hot-news/run', {
      method: 'POST',
      body,
      timeoutMs: 120_000,
    })
  },

  listRuns(query: ListHotNewsRunsQuery = {}) {
    return request<HotNewsRunListResponse>(`${BASE}/runs`, { query: { ...query } })
  },

  runDetail(runId: string) {
    return request<HotNewsRunDetailResponse>(`${BASE}/runs/${runId}`)
  },

  recordDecision(body: RecordHotNewsDecisionRequest) {
    return request<RecordHotNewsDecisionResponse>(`${BASE}/decisions`, {
      method: 'POST',
      body,
    })
  },
}
