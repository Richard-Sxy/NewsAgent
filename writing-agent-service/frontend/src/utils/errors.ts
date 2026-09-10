import { ApiError } from '@/api/http'

/**
 * FastAPI 的 `detail` 既可能是字符串，也可能是校验错误数组。
 * 这里统一压成一句人能读懂的话，避免页面上出现 `[object Object]`。
 */
function describeDetail(detail: unknown): string {
  if (detail === null || detail === undefined) return '服务端未返回附加信息'
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const parts = detail
      .map((entry) => {
        if (entry && typeof entry === 'object' && 'msg' in entry) {
          const record = entry as { loc?: unknown; msg?: unknown }
          const location = Array.isArray(record.loc)
            ? record.loc.filter((part) => part !== 'body').join('.')
            : ''
          const message = typeof record.msg === 'string' ? record.msg : JSON.stringify(record.msg)
          return location ? `${location}: ${message}` : message
        }
        try {
          return JSON.stringify(entry)
        } catch {
          return String(entry)
        }
      })
      .filter((part): part is string => Boolean(part))
    return parts.length ? parts.join('；') : '服务端返回了空的校验错误'
  }
  try {
    return JSON.stringify(detail)
  } catch {
    return String(detail)
  }
}

/** 把任意异常转成可直接展示的中文描述。 */
export function describeError(cause: unknown): string {
  if (cause instanceof ApiError) {
    switch (cause.status) {
      case 400:
        return `请求不被接受（HTTP 400）：${describeDetail(cause.detail)}`
      case 401:
        return '未通过网关鉴权（HTTP 401），请在网关侧重新登录后刷新页面'
      case 403:
        return '权限不足（HTTP 403），当前账号缺少该操作所需的角色'
      case 404:
        return `资源不存在（HTTP 404）：${describeDetail(cause.detail)}`
      case 409:
        return `状态冲突（HTTP 409）：${describeDetail(cause.detail)}`
      case 422:
        return `参数校验未通过（HTTP 422）：${describeDetail(cause.detail)}`
      case 429:
        return '请求过于频繁（HTTP 429），请稍后重试'
      default:
        if (cause.status >= 500) {
          return `服务端错误（HTTP ${cause.status}）：${describeDetail(cause.detail)}`
        }
        return `请求失败（HTTP ${cause.status}）：${describeDetail(cause.detail)}`
    }
  }
  if (cause instanceof DOMException && cause.name === 'AbortError') {
    return '请求已取消或超时'
  }
  if (cause instanceof Error) return cause.message
  return String(cause)
}
