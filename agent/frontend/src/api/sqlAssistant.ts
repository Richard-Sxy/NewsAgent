import { request } from '@/api/http'

export interface SqlAssistantStage {
  name: string
  status: 'passed' | 'blocked' | 'degraded'
  detail: string
  elapsed_ms: number
  attempts: number
}

export interface SqlAssistantScenario {
  id: string
  name: string
  description: string
  result_mode: 'ranking' | 'trend'
  sample_questions: string[]
}

export interface SqlAssistantConfig {
  schema_version: string
  schema_sha256: string
  schema_markdown: string
  dataset: {
    window_start: string
    window_end: string
    news_count: number
    metric_row_count: number
    dataset_profile?: string
    dataset_version?: string
    dataset_sha256?: string
    headline_catalog_count?: number
    headline_catalog_sha256?: string
    headline_date_start?: string
    headline_date_end?: string
    enterprise_scenarios?: unknown[]
  }
  scenarios: SqlAssistantScenario[]
  configuration_template: string
  model_provider: string
}

export interface SqlAssistantPreviewRequest {
  question: string
  scenario_id: string
  window_start: string
  window_end: string
}

export interface SqlAssistantPreview {
  query_id: string
  question: string
  scenario_id: string
  sql: string
  parameters: Record<string, string | number>
  sql_hash: string
  schema_version: string
  schema_sha256: string
  explanation: string
  model_request_id: string | null
  model_provider: string
  expires_at: string
  stages: SqlAssistantStage[]
}

export interface SqlAssistantResult {
  query_id: string
  columns: string[]
  rows: Record<string, string | number | null>[]
  row_count: number
  elapsed_ms: number
  truncated: boolean
  summary: string
  stages: SqlAssistantStage[]
  sql_hash: string
}

const BASE = '/api/v1/sql-assistant'

export const sqlAssistantApi = {
  config() {
    return request<SqlAssistantConfig>(`${BASE}/config`)
  },
  preview(body: SqlAssistantPreviewRequest) {
    return request<SqlAssistantPreview>(`${BASE}/preview`, {
      method: 'POST',
      body,
      timeoutMs: 60_000,
    })
  },
  execute(queryId: string) {
    return request<SqlAssistantResult>(`${BASE}/queries/${encodeURIComponent(queryId)}/execute`, {
      method: 'POST',
      timeoutMs: 60_000,
    })
  },
}
