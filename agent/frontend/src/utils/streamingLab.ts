import type { LabSnapshot, MetricKey } from '@/api/streamingLab'

export function windowRevisions(rows: LabSnapshot[], tenant: string, window: string): LabSnapshot[] {
  return rows.filter((r) => r.tenant_id === tenant && r.window_key === window).sort((a, b) => b.revision - a.revision)
}
export function previousRevision(rows: LabSnapshot[], selected: LabSnapshot): LabSnapshot | undefined {
  return windowRevisions(rows, selected.tenant_id, selected.window_key).find((r) => r.revision < selected.revision)
}
export function metricDelta(current: LabSnapshot, previous: LabSnapshot | undefined, newsId: string, metric: MetricKey): number | null {
  if (!previous || previous.window_key !== current.window_key || previous.tenant_id !== current.tenant_id) return null
  const before = previous.snapshot.metrics.find((r) => r.news_id === newsId)
  const after = current.snapshot.metrics.find((r) => r.news_id === newsId)
  return before && after ? after[metric] - before[metric] : null
}
export function consumptionLabel(row: LabSnapshot): string {
  if (row.invalidated) return '窗口已失效'
  return ({ pending: '等待调度', started: '分析中', completed: row.report ? '分析完成' : '报告缺失', failed: '分析失败' } as Record<string, string>)[row.state] ?? '状态未知'
}
