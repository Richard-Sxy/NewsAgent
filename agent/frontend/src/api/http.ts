import { runtimeConfig } from '@/config/runtime'

/** 后端统一错误响应体：FastAPI 的 detail 既可能是字符串也可能是结构化对象。 */
export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, detail: unknown, message?: string) {
    super(message ?? `${status} ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  query?: QueryParams
  body?: unknown
  signal?: AbortSignal
  timeoutMs?: number
  accept?: string
}

export type QueryValue = string | number | boolean | undefined | null | Array<string | number>

export type QueryParams = Record<string, QueryValue>

const DEFAULT_TIMEOUT_MS = 30_000

/** 认证完全由网关负责：浏览器只带会话 Cookie，不持有网关共享 Token。 */
const CREDENTIALS: RequestCredentials = 'include'

export function buildUrl(path: string, query?: QueryParams): string {
  const url = new URL(`${runtimeConfig().apiBaseUrl}${path}`, window.location.origin)
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null || value === '') continue
      if (Array.isArray(value)) {
        value.forEach((item) => url.searchParams.append(key, String(item)))
      } else {
        url.searchParams.set(key, String(value))
      }
    }
  }
  return url.toString()
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), options.timeoutMs ?? DEFAULT_TIMEOUT_MS)
  const relayAbort = () => controller.abort()
  options.signal?.addEventListener('abort', relayAbort)

  try {
    const response = await fetch(buildUrl(path, options.query), {
      method: options.method ?? 'GET',
      headers: {
        Accept: options.accept ?? 'application/json',
        ...(options.body === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: CREDENTIALS,
      signal: controller.signal,
    })
    return await parseBody<T>(response)
  } finally {
    window.clearTimeout(timer)
    options.signal?.removeEventListener('abort', relayAbort)
  }
}

/** 导出接口返回的是文件流，不能按 JSON 解析。 */
export async function requestBlob(path: string, options: RequestOptions = {}): Promise<Blob> {
  const response = await fetch(buildUrl(path, options.query), {
    method: options.method ?? 'GET',
    headers: { Accept: options.accept ?? '*/*' },
    credentials: CREDENTIALS,
    signal: options.signal,
  })
  if (!response.ok) {
    await parseBody<unknown>(response).catch(() => undefined)
    throw new ApiError(response.status, 'export failed')
  }
  return response.blob()
}

async function parseBody<T>(response: Response): Promise<T> {
  const text = await response.text()
  let payload: unknown = null
  let parsedAsJson = false
  if (text) {
    try {
      payload = JSON.parse(text)
      parsedAsJson = true
    } catch {
      payload = text
    }
  }
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) {
      // 网关负责跳登录；前端只广播事件，避免在 SPA 内自建登录态。
      window.dispatchEvent(new CustomEvent('newsagent:unauthorized', { detail: response.status }))
    }
    const detail = (payload as { detail?: unknown } | null)?.detail ?? payload
    throw new ApiError(response.status, detail)
  }
  // 网关登录页或反向代理的 SPA 兜底会以 200 返回 HTML。若不拦住，
  // 调用方会拿到一个字符串当成对象用，报出难以定位的运行时错误。
  if (text && !parsedAsJson) {
    throw new ApiError(
      response.status,
      '预期 JSON 响应，实际收到非 JSON 内容：请求可能被网关登录页或反向代理拦截',
    )
  }
  return payload as T
}
