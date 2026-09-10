import { ApiError, buildUrl } from '@/api/http'
import type { ProgressEvent } from '@/api/types'

export interface JobEventStreamOptions {
  jobId: string
  /** 断线重连时从这里续读，避免丢事件。 */
  lastEventId?: string
  signal?: AbortSignal
  onEvent: (event: ProgressEvent, frame: { id: string | null; name: string }) => void
  onOpen?: () => void
  onError?: (error: unknown, attempt: number) => void
}

export interface JobEventStreamHandle {
  close: () => void
}

const MAX_BACKOFF_MS = 15_000

/**
 * 手动消费 SSE。
 *
 * 不使用原生 EventSource 有两个原因：
 * 1. 需要显式发送 `Last-Event-ID` 并在断线后从游标续读；
 * 2. 需要可控的中止语义与自定义退避策略。
 * 身份仍由网关负责，这里只带会话 Cookie。
 */
export function openJobEventStream(options: JobEventStreamOptions): JobEventStreamHandle {
  const controller = new AbortController()
  const relayAbort = () => controller.abort()
  options.signal?.addEventListener('abort', relayAbort)

  void consume(options, controller.signal).finally(() => {
    options.signal?.removeEventListener('abort', relayAbort)
  })

  return { close: () => controller.abort() }
}

async function consume(options: JobEventStreamOptions, signal: AbortSignal): Promise<void> {
  let cursor = options.lastEventId ?? ''
  let attempt = 0

  for (;;) {
    if (signal.aborted) return
    try {
      await pump(options, signal, () => cursor, (value) => (cursor = value))
      attempt = 0
      return
    } catch (error) {
      if (signal.aborted || isAbortError(error)) return
      attempt += 1
      options.onError?.(error, attempt)
      await sleep(Math.min(500 * 2 ** (attempt - 1), MAX_BACKOFF_MS), signal)
    }
  }
}

async function pump(
  options: JobEventStreamOptions,
  signal: AbortSignal,
  readCursor: () => string,
  writeCursor: (value: string) => void,
): Promise<void> {
  const cursor = readCursor()
  const response = await fetch(buildUrl(`/api/v1/jobs/${options.jobId}/events`), {
    headers: {
      Accept: 'text/event-stream',
      ...(cursor ? { 'Last-Event-ID': cursor } : {}),
    },
    credentials: 'include',
    signal,
  })

  if (!response.ok || !response.body) {
    throw new ApiError(response.status, 'event stream unavailable')
  }

  options.onOpen?.()

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  for (;;) {
    const { done, value } = await reader.read()
    if (done) return
    buffer += decoder.decode(value, { stream: true })

    let boundary = buffer.indexOf('\n\n')
    while (boundary !== -1) {
      const chunk = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      const frame = parseFrame(chunk)
      if (frame) {
        if (frame.id) writeCursor(frame.id)
        if (frame.data) {
          try {
            options.onEvent(JSON.parse(frame.data) as ProgressEvent, {
              id: frame.id,
              name: frame.name,
            })
          } catch {
            // 单帧解析失败不应中断整条流。
          }
        }
      }
      boundary = buffer.indexOf('\n\n')
    }
  }
}

interface SseFrame {
  id: string | null
  name: string
  data: string
}

/** 心跳与注释帧（以 `:` 开头）返回 null，由调用方直接跳过。 */
function parseFrame(chunk: string): SseFrame | null {
  let id: string | null = null
  let name = 'message'
  const dataLines: string[] = []

  for (const line of chunk.split('\n')) {
    if (!line || line.startsWith(':')) continue
    if (line.startsWith('id:')) id = line.slice(3).trim()
    else if (line.startsWith('event:')) name = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
  }

  if (!id && name === 'message' && dataLines.length === 0) return null
  return { id, name, data: dataLines.join('\n') }
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError'
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = window.setTimeout(resolve, ms)
    signal.addEventListener(
      'abort',
      () => {
        window.clearTimeout(timer)
        resolve()
      },
      { once: true },
    )
  })
}
