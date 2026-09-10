/** 生成一次提交幂等键；后端要求长度 8-128。 */
export function newIdempotencyKey(prefix: string): string {
  const random =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
  return `${prefix}-${random}`.slice(0, 128)
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', { hour12: false })
}

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return '-'
  return `${value}%`
}

export function truncate(value: string, max = 80): string {
  return value.length <= max ? value : `${value.slice(0, max)}…`
}
