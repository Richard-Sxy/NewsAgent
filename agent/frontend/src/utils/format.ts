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
  const formatted = formatDecimal(value, 2)
  return formatted === '—' || formatted === '见权威答复' ? formatted : `${formatted}%`
}

export type NumericValue = number | string | null | undefined

/** 只格式化显示，不修改后端原值；权威 decimal 字符串不转换为浮点数。 */
function decimalParts(value: NumericValue): { negative: boolean; whole: string; fraction: string } | null {
  if (value === null || value === undefined) return null
  if (typeof value === 'number' && (!Number.isFinite(value) || Number.isInteger(value) && !Number.isSafeInteger(value))) return null
  let raw = String(value)
  // JSON 的有限小数可能以科学计数法送达，指数仅用于移动字符位置。
  if (typeof value === 'number' && /e/i.test(raw)) {
    const scientific = /^(-?)(\d+)(?:\.(\d+))?e([+-]?\d+)$/i.exec(raw)
    if (!scientific) return null
    const digits = scientific[2]! + (scientific[3] ?? '')
    const point = scientific[2]!.length + Number(scientific[4])
    raw = scientific[1] + (point <= 0 ? `0.${'0'.repeat(-point)}${digits}`
      : point >= digits.length ? digits + '0'.repeat(point - digits.length)
        : `${digits.slice(0, point)}.${digits.slice(point)}`)
  }
  if (raw.length > 400) return null
  const parts = /^(-?)(\d+)(?:\.(\d+))?$/.exec(raw)
  return parts ? { negative: parts[1] === '-', whole: parts[2]!.replace(/^0+(?=\d)/, ''), fraction: parts[3] ?? '' } : null
}

function roundedDecimal(value: NumericValue, places: number, shift = 0, fixed = false): string {
  if (typeof value === 'number' && Number.isInteger(value) && !Number.isSafeInteger(value)) return '见权威答复'
  const parts = decimalParts(value)
  if (!parts) return '—'
  const fraction = parts.fraction.padEnd(shift, '0')
  const whole = parts.whole + fraction.slice(0, shift)
  const remainder = fraction.slice(shift)
  let digits = BigInt(whole + remainder.slice(0, places).padEnd(places, '0'))
  if ((remainder[places] ?? '0') >= '5') digits += 1n
  const padded = String(digits).padStart(places + 1, '0')
  const integer = places ? padded.slice(0, -places) : padded
  const decimals = places ? padded.slice(-places) : ''
  const shown = fixed ? decimals : decimals.replace(/0+$/, '')
  return `${parts.negative && digits !== 0n ? '-' : ''}${integer}${shown ? `.${shown}` : ''}`
}

export function formatDecimal(value: NumericValue, places = 2): string {
  if (!Number.isInteger(places) || places < 0 || places > 12) return '—'
  return roundedDecimal(value, places)
}

/** 后端比值 → 百分比，保留两位；0.12345 显示为 12.35%。 */
export function formatRatioPercent(value: NumericValue): string {
  const formatted = roundedDecimal(value, 2, 2, true)
  return formatted === '—' || formatted === '见权威答复' ? formatted : `${formatted}%`
}

/** SQL 展示列有固定语义，新闻ID/标题/时间等文本不当作数字处理。 */
export function formatMetricCell(value: NumericValue, column: string): string {
  if (value === null || value === undefined) return '—'
  const metric = column.replace(/^baseline_/, '')
  if (['ctr', 'click_through_rate', 'effective_consumption_rate', 'interaction_rate',
    'relative_change', 'click_growth'].includes(metric)) return formatRatioPercent(value)
  if (metric === 'hot_score' || metric === 'score' || metric.endsWith('_component')) return formatDecimal(value, 4)
  if (typeof value === 'number' || /^(?:baseline_|avg_|total_)?(?:impressions|clicks|unique_users|effective_consumptions|interactions|duration_seconds|dwell_seconds|duration|dwell|news_count|rank|current_value|reference_value|absolute_change|mean|median|p90)$/.test(column)
      || column.startsWith('baseline_') || column.endsWith('_seconds')) return formatDecimal(value)
  return String(value)
}

export function truncate(value: string, max = 80): string {
  return value.length <= max ? value : `${value.slice(0, max)}…`
}
