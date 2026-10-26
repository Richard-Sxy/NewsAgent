export const LAB_TENANTS = ['11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222']
export const METRIC_LABELS = {
  impressions: '曝光', clicks: '点击', unique_users: '点击人数',
  total_duration_seconds: '消费时长（秒）', effective_consumptions: '有效消费', interactions: '互动',
}
export type MetricKey = keyof typeof METRIC_LABELS
export type Metrics = Record<MetricKey, number> & { news_id: string; content_type: string }
export interface LabSnapshot {
  snapshot_id: string; window_key: string; tenant_id: string; revision: number; final: boolean
  state: string; workflow_id: string; error_type: string | null; invalidated: boolean; is_latest: boolean
  base_snapshot_id: string | null; backfill_request_id: string | null; added_event_count: number | null; backfill_reason: string | null
  snapshot: { window_start_ms: number; window_end_ms: number; event_count: number; metric_version: string; metrics: Metrics[] }
  report: null | {
    run_key: string; trace_id: string; model_mode: string; limitations: string[]
    ranking: { rank: number; news_id: string; hot_score: string; metrics: Metrics }[]
    analyses: { news_id: string; model_request_id: string; report: { trend_assessment: string; limitations: string[] } }[]
  }
}
export interface LabRun {
  run_id: string; snapshots: LabSnapshot[]
  rejections: { tenant_id: string; event_id: string; reason: string }[]
  rejections_truncated: boolean
}
export interface LabRunSummary { run_id: string; updated_at: string; snapshot_count: number }
export interface LabOperations {
  invalidated_failures?: number
  wallclock: null | { id: string; enabled: boolean; last_error: string | null; reader_lag_seconds: number | null }
  dynamic_feed: {
    enabled: boolean; interval_seconds: number; max_windows: number; sequence: number; last_error: string | null; updated_at: string
    recent_batches: { sequence: number; run_id: string; scenario: string; state: string; event_count: number; window_start_ms: number; source_trace_id: string | null; completed_at: string | null }[]
  }
  dispatcher: { state: string; last_success_at: string | null; error_type: string | null; consecutive_failures: number }
  ledger: { pending: number; started: number; completed: number; failed: number }
  oldest_pending_seconds: number; invalid_windows: number; rejected_events: number; backfills: number
  publication_to_report: { report_count: number; seconds: number[] | null }
}

export interface LiveNews {
  window_key: string; revision: number; tenant_id: string; run_id: string; news_id: string; title: string
  window_start: string; window_end: string; impressions: number; clicks: number; unique_users: number
  reader_count: number | null; final: boolean; source_check_fresh: boolean; projected_at: string
  latest_event_to_sql_seconds: number; reader_profile_status: string
  reader_profile: null | { short_term: LiveInterest; long_term: LiveInterest }
}
export interface LiveInterest {
  profiled_reader_count: number; unknown_reader_count: number; cold_start_reader_count: number
  topics: { topic_id: string; reader_count: number; share_profiled: string; lift: string | null; low_sample: boolean }[]
}

async function read<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/streaming-lab-api${path}`, { signal, cache: 'no-store', credentials: 'same-origin' })
  if (!response.ok) throw new Error(`流式实验查询失败（HTTP ${response.status}）`)
  return await response.json() as T
}
export const streamingLab = {
  realtime: (tenant: string, signal?: AbortSignal) => read<{ items: LiveNews[]; topic_names: Record<string, string> }>(`/realtime-news?tenant_id=${encodeURIComponent(tenant)}&limit=6`, signal),
  operations: (signal?: AbortSignal) => read<LabOperations>('/operations', signal),
  runs: (signal?: AbortSignal) => read<{ runs: LabRunSummary[] }>('/runs', signal),
  run: (id: string, tenant: string, signal?: AbortSignal) => read<LabRun>(`/runs/${encodeURIComponent(id)}?tenant_id=${encodeURIComponent(tenant)}`, signal),
}
