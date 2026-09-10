/**
 * 轻量表单校验工具。
 *
 * 每个函数返回 `null` 表示通过，返回字符串表示错误原因。
 * 视图里用 `firstError(...)` 聚合，保证一次只展示最靠前的那条问题。
 */

/** 带时区的 ISO 8601，后端对 Data Loop 的 window_start / window_end 有此硬要求。 */
const ISO_WITH_TIMEZONE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/

export function requiredText(value: string, label: string): string | null {
  return value.trim().length > 0 ? null : `${label}不能为空`
}

export function minLength(value: string, length: number, label: string): string | null {
  return value.trim().length >= length ? null : `${label}至少需要 ${length} 个字符`
}

export function isoDateTimeWithTimezone(value: string, label: string): string | null {
  const text = value.trim()
  if (!text) return `${label}不能为空`
  if (!ISO_WITH_TIMEZONE.test(text)) {
    return `${label}必须是带时区的 ISO 8601，例如 2026-09-10T00:00:00+08:00`
  }
  if (Number.isNaN(new Date(text).getTime())) return `${label}不是合法时间`
  return null
}

interface JsonOptions {
  allowEmpty?: boolean
  expectArray?: boolean
}

export function jsonText(value: string, label: string, options: JsonOptions = {}): string | null {
  const text = value.trim()
  if (!text) {
    return options.allowEmpty ? null : `${label}不能为空`
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch (cause) {
    return `${label}不是合法 JSON：${cause instanceof Error ? cause.message : String(cause)}`
  }
  if (parsed === null || typeof parsed !== 'object') {
    return options.expectArray ? `${label}必须是 JSON 数组` : `${label}必须是 JSON 对象`
  }
  if (options.expectArray && !Array.isArray(parsed)) return `${label}必须是 JSON 数组`
  if (!options.expectArray && Array.isArray(parsed)) return `${label}必须是 JSON 对象，不能是数组`
  return null
}

/** 解析 JSON 文本，调用前应先用 `jsonText` 校验。 */
export function parseJson<T>(value: string, fallback: T): T {
  const text = value.trim()
  if (!text) return fallback
  try {
    return JSON.parse(text) as T
  } catch {
    return fallback
  }
}

export function firstError(...results: Array<string | null>): string | null {
  for (const result of results) {
    if (result !== null) return result
  }
  return null
}

/** 把多行文本拆成去重后的非空数组，用于「每行一个 id」这类输入。 */
export function toLineList(value: string): string[] {
  const seen = new Set<string>()
  for (const line of value.split('\n')) {
    const trimmed = line.trim()
    if (trimmed) seen.add(trimmed)
  }
  return [...seen]
}

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

/** 生成 `<input type="datetime-local">` 的默认值（本地时区，分钟精度）。 */
export function toLocalInputValue(date: Date = new Date()): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`
}

/** 把 datetime-local 的值补上浏览器当前时区偏移，转成后端要求的 ISO 8601。 */
export function localInputToIso(value: string): string {
  const text = value.trim()
  if (!text) return ''
  const date = new Date(text)
  if (Number.isNaN(date.getTime())) return text
  const offsetMinutes = -date.getTimezoneOffset()
  const sign = offsetMinutes >= 0 ? '+' : '-'
  const absolute = Math.abs(offsetMinutes)
  // datetime-local 只到分钟，补上秒以符合后端解析习惯。
  const base = text.length === 16 ? `${text}:00` : text
  return `${base}${sign}${pad(Math.floor(absolute / 60))}:${pad(absolute % 60)}`
}
