import { buildUrl, request } from '@/api/http'

export interface HotNewsNotice {
  notice_id: string; monitor_id: string; snapshot_id: string; news_id: string; title: string
  window_start_ms: number; window_end_ms: number; expires_at_ms: number
  hot_score: number; clicks: number; unique_users: number; trend_assessment: string; policy_version: string
}
export interface MonitorView {
  monitor_id: string; window_minutes: 5 | 15; enabled: boolean
  status: 'waiting_source' | 'published' | 'paused' | 'blocked' | 'backpressure'
  source_end_ms: number | null; error_type: string | null
}
export interface MonitorFeed {
  configured: boolean; can_manage: boolean; tenant_id: string; scope: 'isolated_synthetic'
  source_state: 'ready' | 'waiting_source' | 'disabled' | 'unavailable'
  monitors: MonitorView[]; items: HotNewsNotice[]
}
const BASE = '/api/v1/hot-news-monitor'
export const hotNewsMonitorApi = {
  snapshot: (signal?: AbortSignal) => request<MonitorFeed>(BASE, { signal, timeoutMs: 6000 }),
  start: (minutes: 5 | 15) => request<MonitorFeed>(BASE, { method: 'POST', body: { window_minutes: minutes }, timeoutMs: 6000 }),
  switch: (id: string, enabled: boolean) => request<MonitorFeed>(BASE + '/' + encodeURIComponent(id),
    { method: 'PUT', body: { enabled }, timeoutMs: 6000 }),
  subscribe(onFeed: (feed: MonitorFeed) => void, onError: () => void) {
    // Cookie-only browser authentication; gateway injects the trusted identity.
    let source: EventSource
    let stopped = false
    const open = () => {
      if (stopped) return
      const current = source = new EventSource(buildUrl(BASE + '/events'), { withCredentials: true })
      current.addEventListener('hotspots', (event: MessageEvent<string>) => {
        if (stopped || current !== source) return
        try { onFeed(JSON.parse(event.data) as MonitorFeed) } catch { onError() }
      })
      current.addEventListener('reconnect', () => {
        if (stopped || current !== source) return
        current.close()
        open()
      })
      current.onerror = () => { if (!stopped && current === source) onError() }
    }
    open()
    return () => { stopped = true; source.close() }
  },
}

