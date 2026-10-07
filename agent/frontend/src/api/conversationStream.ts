import { ApiError, buildUrl } from '@/api/http'
import type { ConversationTurn, SendConversationMessage } from '@/api/conversations'

export type ConversationStreamEvent =
  | {
      type: 'accepted'
      data: {
        turn_id: string
        request_id: string
        replayed: boolean
        status: ConversationTurn['status']
      }
    }
  | {
      type: 'phase'
      data: { phase: string; message: string; call?: number; attempt?: number; tool_name?: string }
    }
  | { type: 'tool_started'; data: { index: number; name: string } }
  | {
      type: 'tool_finished'
      data: {
        index: number
        name: string
        status: 'completed' | 'failed' | 'denied'
        attempts: number
        error_code: string | null
      }
    }
  | {
      type: 'answer_delta'
      data: { turn_id: string; request_id: string; index: number; text: string }
    }
  | { type: 'done'; data: { turn: ConversationTurn } }

export interface ConversationStreamOptions {
  signal?: AbortSignal
  onEvent: (event: ConversationStreamEvent) => void
  timeoutMs?: number
}

export class ConversationStreamError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message)
    this.name = 'ConversationStreamError'
  }
}

const MAX_BUFFER = 256_000
const MAX_TOTAL = 1_000_000
const MAX_EVENTS = 4_096

function protocolError(): never {
  throw new ConversationStreamError('stream_protocol_invalid', '流式响应格式或事件顺序异常')
}

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) protocolError()
  return value as Record<string, unknown>
}

function string(value: unknown, max = 1_000): string {
  if (typeof value !== 'string' || value.length > max) protocolError()
  return value
}

function integer(value: unknown, max = MAX_EVENTS): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0 || value > max)
    protocolError()
  return value
}

function status(value: unknown, terminal = false): ConversationTurn['status'] {
  if (value === 'completed' || value === 'failed' || (!terminal && value === 'processing'))
    return value
  return protocolError()
}

function toolStatus(value: unknown): 'completed' | 'failed' | 'denied' {
  if (value === 'completed' || value === 'failed' || value === 'denied') return value
  return protocolError()
}

function nullableString(value: unknown): string | null {
  return value === null ? null : string(value)
}

function terminalTurn(value: unknown): ConversationTurn {
  const turn = record(value)
  string(turn.id, 100)
  string(turn.request_id, 100)
  string(turn.user_content, 8_000)
  if (turn.assistant_content !== null) string(turn.assistant_content, 60_000)
  status(turn.status, true)
  nullableString(turn.error_code)
  string(turn.created_at)
  string(turn.completed_at)
  if (!Array.isArray(turn.model_request_ids) || turn.model_request_ids.length > 128) protocolError()
  turn.model_request_ids.forEach((value) => string(value))
  if (!Array.isArray(turn.tools) || turn.tools.length > 6) protocolError()
  for (const item of turn.tools) {
    const trace = record(item)
    string(trace.name, 100)
    toolStatus(trace.status)
    integer(trace.attempts, 2)
    record(trace.arguments)
    record(trace.result)
    nullableString(trace.error_code)
  }
  return turn as unknown as ConversationTurn
}

/** One POST, no automatic reconnect: retries must retain the caller's request_id. */
export async function streamConversationMessage(
  path: string,
  body: SendConversationMessage,
  options: ConversationStreamOptions,
): Promise<ConversationTurn> {
  const controller = new AbortController()
  const relayAbort = () => controller.abort()
  options.signal?.addEventListener('abort', relayAbort, { once: true })
  if (options.signal?.aborted) controller.abort()
  const timer = setTimeout(() => controller.abort(), options.timeoutMs ?? 150_000)
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined

  try {
    const response = await fetch(buildUrl(path), {
      method: 'POST',
      headers: { Accept: 'text/event-stream', 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      credentials: 'include',
      signal: controller.signal,
    })
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        window.dispatchEvent(new CustomEvent('newsagent:unauthorized', { detail: response.status }))
      }
      reader = response.body?.getReader()
      const errorDecoder = new TextDecoder()
      let raw = ''
      for (;;) {
        const part = await reader?.read()
        if (!part || part.done) {
          raw += errorDecoder.decode()
          break
        }
        if (raw.length + part.value.byteLength > 16_000) {
          throw new ApiError(response.status, '网关错误响应超出大小限制')
        }
        raw += errorDecoder.decode(part.value, { stream: true })
      }
      let detail: unknown = raw
      try {
        detail = record(JSON.parse(raw)).detail ?? raw
      } catch {
        /* Non-JSON gateway error. */
      }
      throw new ApiError(response.status, detail)
    }
    if (
      !response.body ||
      response.headers.get('content-type')?.split(';')[0]?.trim() !== 'text/event-stream'
    ) {
      throw new ConversationStreamError(
        'stream_content_type_invalid',
        '预期事件流，请检查网关或服务端响应',
      )
    }
    reader = response.body.getReader()
    const decoder = new TextDecoder('utf-8', { fatal: true })
    let buffer = ''
    let frameName = 'message'
    let dataLines: string[] = []
    let frameSize = 0
    let total = 0
    let eventCount = 0
    let acceptedId: string | null = null
    let nextDelta = 0
    let answer = ''
    let result: ConversationTurn | null = null
    const tools = new Map<number, { name: string; finished: boolean }>()

    function event(name: string, source: string): void {
      if (++eventCount > MAX_EVENTS || result) protocolError()
      let data: Record<string, unknown>
      try {
        data = record(JSON.parse(source))
      } catch {
        return protocolError()
      }
      if (name === 'error') {
        throw new ConversationStreamError(string(data.code, 100), string(data.message))
      }
      if (name === 'accepted') {
        if (acceptedId || data.request_id !== body.request_id || typeof data.replayed !== 'boolean')
          protocolError()
        acceptedId = string(data.turn_id, 100)
        if (!acceptedId) protocolError()
        options.onEvent({
          type: 'accepted',
          data: {
            turn_id: acceptedId,
            request_id: body.request_id,
            replayed: data.replayed,
            status: status(data.status),
          },
        })
        return
      }
      if (!acceptedId) protocolError()
      if (name === 'phase') {
        const phase = {
          phase: string(data.phase, 100),
          message: string(data.message),
          ...(data.call === undefined ? {} : { call: integer(data.call) }),
          ...(data.attempt === undefined ? {} : { attempt: integer(data.attempt) }),
          ...(data.tool_name === undefined ? {} : { tool_name: string(data.tool_name, 100) }),
        }
        options.onEvent({ type: 'phase', data: phase })
      } else if (name === 'tool_started') {
        const index = integer(data.index, 5)
        const toolName = string(data.name, 100)
        if (index !== tools.size || tools.has(index)) protocolError()
        tools.set(index, { name: toolName, finished: false })
        options.onEvent({ type: 'tool_started', data: { index, name: toolName } })
      } else if (name === 'tool_finished') {
        const index = integer(data.index, 5)
        const toolName = string(data.name, 100)
        const current = tools.get(index)
        if (!current || current.finished || current.name !== toolName) protocolError()
        current.finished = true
        options.onEvent({
          type: 'tool_finished',
          data: {
            index,
            name: toolName,
            status: toolStatus(data.status),
            attempts: integer(data.attempts, 2),
            error_code: nullableString(data.error_code),
          },
        })
      } else if (name === 'answer_delta') {
        const index = integer(data.index)
        const text = string(data.text, 60_000)
        if (
          data.turn_id !== acceptedId ||
          data.request_id !== body.request_id ||
          index !== nextDelta ||
          !text
        )
          protocolError()
        if (answer.length + text.length > 60_000) protocolError()
        answer += text
        nextDelta += 1
        options.onEvent({
          type: 'answer_delta',
          data: { turn_id: acceptedId, request_id: body.request_id, index, text },
        })
      } else if (name === 'done') {
        const turn = terminalTurn(data.turn)
        if (
          turn.id !== acceptedId ||
          turn.request_id !== body.request_id ||
          turn.user_content !== body.content.trim() ||
          (nextDelta && answer !== (turn.assistant_content ?? ''))
        )
          protocolError()
        result = turn
        options.onEvent({ type: 'done', data: { turn } })
      } else protocolError()
    }

    function line(value: string): void {
      if (!value) {
        if (dataLines.length) event(frameName, dataLines.join('\n'))
        frameName = 'message'
        dataLines = []
        frameSize = 0
        return
      }
      frameSize += value.length
      if (frameSize > MAX_BUFFER) protocolError()
      if (value.startsWith(':')) return
      const colon = value.indexOf(':')
      const field = colon < 0 ? value : value.slice(0, colon)
      let content = colon < 0 ? '' : value.slice(colon + 1)
      if (content.startsWith(' ')) content = content.slice(1)
      if (field === 'event') frameName = string(content, 100)
      else if (field === 'data') dataLines.push(content)
    }

    function drain(final = false): void {
      for (;;) {
        const boundary = buffer.search(/[\r\n]/)
        if (boundary < 0 || (!final && buffer[boundary] === '\r' && boundary === buffer.length - 1))
          return
        const width = buffer[boundary] === '\r' && buffer[boundary + 1] === '\n' ? 2 : 1
        const value = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + width)
        line(value)
      }
    }

    for (;;) {
      if (controller.signal.aborted) throw new DOMException('请求已取消或超时', 'AbortError')
      const chunk = await reader.read()
      total += chunk.value?.byteLength ?? 0
      if (total > MAX_TOTAL) protocolError()
      // A network chunk may aggregate many valid SSE frames. Bound unfinished
      // frame state while decoding smaller slices, rather than rejecting the
      // aggregate as if it were one large event.
      const bytes = chunk.value ?? new Uint8Array()
      for (let offset = 0; offset < bytes.length || chunk.done; offset += 16_384) {
        try {
          buffer += decoder.decode(bytes.subarray(offset, offset + 16_384), { stream: !chunk.done })
        } catch {
          throw new ConversationStreamError('stream_utf8_invalid', '流式响应包含无效的 UTF-8 文本')
        }
        drain(chunk.done)
        if (buffer.length + frameSize > MAX_BUFFER) protocolError()
        if (chunk.done) break
      }
      if (result) return result
      if (chunk.done) {
        throw new ConversationStreamError(
          'stream_incomplete',
          '连接已结束，尚未收到已保存的最终结果',
        )
      }
    }
  } finally {
    clearTimeout(timer)
    options.signal?.removeEventListener('abort', relayAbort)
    controller.abort()
    await reader?.cancel().catch(() => undefined)
  }
}
