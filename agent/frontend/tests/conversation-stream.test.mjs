import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { ReadableStream } from 'node:stream/web'
import test from 'node:test'
import { setImmediate } from 'node:timers/promises'
import { TextEncoder } from 'node:util'

import ts from 'typescript'

const source = readFileSync(new URL('../src/api/conversationStream.ts', import.meta.url), 'utf8')
const encoder = new TextEncoder()
const REQUEST = { request_id: 'request-fixed', content: '问题' }
const TURN = {
  id: 'turn-fixed',
  request_id: REQUEST.request_id,
  user_content: REQUEST.content,
  assistant_content: '中文🙂<img src=x onerror=alert(1)>',
  status: 'completed',
  tools: [],
  model_request_ids: ['model-saved'],
  error_code: null,
  created_at: '2026-10-04T00:00:00Z',
  completed_at: '2026-10-04T00:00:01Z',
}

class ApiError extends Error {
  constructor(status, detail) {
    super(String(detail))
    this.status = status
    this.detail = detail
  }
}

function load(fetch, dispatched = []) {
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  })
  const exports = {}
  new Function('require', 'exports', 'fetch', 'window', 'CustomEvent', outputText)(
    () => ({ ApiError, buildUrl: (path) => `http://local.test${path}` }),
    exports,
    fetch,
    { dispatchEvent: (event) => dispatched.push(event) },
    class {
      constructor(type, options) {
        this.type = type
        this.detail = options.detail
      }
    },
  )
  return exports
}

function frame(name, data, crlf = false, multiline = false) {
  const newline = crlf ? '\r\n' : '\n'
  const lines = JSON.stringify(data, null, multiline ? 2 : undefined).split('\n')
  return [`event: ${name}`, ...lines.map((line) => `data: ${line}`), '', ''].join(newline)
}

function accepted(replayed = false) {
  return frame('accepted', {
    turn_id: TURN.id,
    request_id: REQUEST.request_id,
    replayed,
    status: replayed ? 'completed' : 'processing',
  })
}

function delta(index = 0, text = TURN.assistant_content, values = {}) {
  return frame('answer_delta', {
    turn_id: TURN.id,
    request_id: REQUEST.request_id,
    index,
    text,
    ...values,
  })
}

function response(text, byteSize = 1_000_000) {
  const bytes = typeof text === 'string' ? encoder.encode(text) : text
  return new globalThis.Response(
    new ReadableStream({
      start(controller) {
        for (let offset = 0; offset < bytes.length; offset += byteSize)
          controller.enqueue(bytes.slice(offset, offset + byteSize))
        controller.close()
      },
    }),
    { headers: { 'Content-Type': 'text/event-stream; charset=utf-8' } },
  )
}

test('UTF-8 split at every byte, CRLF, multiline data and heartbeats retain exact safe text', async () => {
  const events = []
  let options
  const text =
    ': heartbeat\r\n\r\n' +
    frame(
      'accepted',
      { turn_id: TURN.id, request_id: REQUEST.request_id, replayed: false, status: 'processing' },
      true,
      true,
    ) +
    frame('phase', { phase: 'planning', message: '正在规划🙂', call: 1, attempt: 1 }, true, true) +
    frame('tool_started', { index: 0, name: 'read_hot_news' }, true) +
    frame(
      'tool_finished',
      { index: 0, name: 'read_hot_news', status: 'completed', attempts: 1, error_code: null },
      true,
    ) +
    delta() +
    frame('done', { turn: TURN }, true, true)
  const { streamConversationMessage } = load(async (_url, value) => {
    options = value
    return response(text, 1)
  })
  const result = await streamConversationMessage('/stream', REQUEST, {
    onEvent: (event) => events.push(event),
  })
  assert.deepEqual(result, TURN)
  assert.equal(
    events.find((event) => event.type === 'answer_delta').data.text,
    TURN.assistant_content,
  )
  assert.deepEqual(
    events.map((event) => event.type),
    ['accepted', 'phase', 'tool_started', 'tool_finished', 'answer_delta', 'done'],
  )
  assert.equal(options.credentials, 'include')
  assert.deepEqual(options.headers, {
    Accept: 'text/event-stream',
    'Content-Type': 'application/json',
  })
  assert.deepEqual(JSON.parse(options.body), REQUEST)
})

test('a delta callback is visible while done is still waiting on the network', async () => {
  let controller
  let completed = false
  const events = []
  const stream = new ReadableStream({
    start(value) {
      controller = value
    },
  })
  const { streamConversationMessage } = load(
    async () =>
      new globalThis.Response(stream, { headers: { 'Content-Type': 'text/event-stream' } }),
  )
  const pending = streamConversationMessage('/stream', REQUEST, {
    onEvent: (event) => events.push(event),
  }).then((value) => {
    completed = true
    return value
  })
  controller.enqueue(encoder.encode(accepted() + delta()))
  await setImmediate()
  assert.equal(events.at(-1).type, 'answer_delta')
  assert.equal(completed, false)
  controller.enqueue(encoder.encode(frame('done', { turn: TURN })))
  controller.close()
  assert.deepEqual(await pending, TURN)
})

test('JSON authorization and pre-stream errors preserve status and broadcast gateway failure', async (t) => {
  for (const status of [401, 403, 404, 409, 422, 429, 503]) {
    await t.test(String(status), async () => {
      const dispatched = []
      const { streamConversationMessage } = load(
        async () =>
          new globalThis.Response(JSON.stringify({ detail: 'gateway-detail' }), {
            status,
            headers: { 'Content-Type': 'application/json' },
          }),
        dispatched,
      )
      await assert.rejects(
        streamConversationMessage('/stream', REQUEST, {
          onEvent: () => assert.fail('JSON failure cannot emit SSE'),
        }),
        (error) =>
          error instanceof ApiError && error.status === status && error.detail === 'gateway-detail',
      )
      assert.equal(dispatched.length, status === 401 || status === 403 ? 1 : 0)
      if (dispatched.length) assert.equal(dispatched[0].detail, status)
    })
  }
})

test('an HTML login fallback is not parsed as a successful event stream', async () => {
  const { streamConversationMessage } = load(
    async () =>
      new globalThis.Response('<html>login</html>', { headers: { 'Content-Type': 'text/html' } }),
  )
  await assert.rejects(
    streamConversationMessage('/stream', REQUEST, { onEvent: () => {} }),
    (error) => error.code === 'stream_content_type_invalid',
  )
})

test('a stream ending without done fails without automatically replaying the POST', async () => {
  let requests = 0
  const { streamConversationMessage } = load(async () => {
    requests += 1
    return response(accepted() + delta())
  })
  await assert.rejects(
    streamConversationMessage('/stream', REQUEST, { onEvent: () => {} }),
    (error) => error.code === 'stream_incomplete',
  )
  assert.equal(requests, 1)
})

test('saved terminal replay returns the same audited answer without planning or tool events', async () => {
  const events = []
  const { streamConversationMessage } = load(async () =>
    response(accepted(true) + delta() + frame('done', { turn: TURN })),
  )
  const value = await streamConversationMessage('/stream', REQUEST, {
    onEvent: (event) => events.push(event),
  })
  assert.equal(events[0].data.replayed, true)
  assert.deepEqual(value.model_request_ids, TURN.model_request_ids)
  assert.deepEqual(value, TURN)
  assert.deepEqual(
    events.map((event) => event.type),
    ['accepted', 'answer_delta', 'done'],
  )
})

test('duplicate, reordered or mismatched response events fail closed', async (t) => {
  const cases = {
    duplicate: accepted() + delta(0, 'a') + delta(0, 'a'),
    reordered: accepted() + delta(1, 'a'),
    wrong_request: accepted() + delta(0, 'a', { request_id: 'another-request' }),
    wrong_turn: accepted() + delta(0, 'a', { turn_id: 'another-turn' }),
    delta_before_accept: delta(),
    duplicate_accept: accepted() + accepted(),
    answer_mismatch: accepted() + delta(0, 'changed text') + frame('done', { turn: TURN }),
    nonterminal_done:
      accepted() + frame('done', { turn: { ...TURN, status: 'processing', completed_at: null } }),
    wrong_done: accepted() + frame('done', { turn: { ...TURN, request_id: 'another-request' } }),
    unknown_event: accepted() + frame('execute_command', { content: 'external instruction' }),
    duplicate_done: accepted() + frame('done', { turn: TURN }) + frame('done', { turn: TURN }),
    tool_without_start:
      accepted() +
      frame('tool_finished', {
        index: 0,
        name: 'read_hot_news',
        status: 'completed',
        attempts: 1,
        error_code: null,
      }),
  }
  for (const [name, text] of Object.entries(cases)) {
    await t.test(name, async () => {
      const { streamConversationMessage } = load(async () => response(text))
      await assert.rejects(
        streamConversationMessage('/stream', REQUEST, { onEvent: () => {} }),
        (error) => error.code === 'stream_protocol_invalid',
      )
    })
  }
})

test('server error events preserve explicit failure instead of accepting partial answer as final', async () => {
  const { streamConversationMessage } = load(async () =>
    response(accepted() + delta() + frame('error', { code: 'interrupted', message: '本轮已中断' })),
  )
  await assert.rejects(
    streamConversationMessage('/stream', REQUEST, { onEvent: () => {} }),
    (error) => error.code === 'interrupted' && error.message === '本轮已中断',
  )
})

test('invalid UTF-8, malformed JSON and oversized unfinished frames are bounded failures', async (t) => {
  const cases = [
    ['utf8', new Uint8Array([0xff, 0xfe]), 'stream_utf8_invalid'],
    ['truncated_utf8', new Uint8Array([0xe4, 0xb8]), 'stream_utf8_invalid'],
    ['json', 'event: accepted\ndata: {broken}\n\n', 'stream_protocol_invalid'],
    ['buffer', `data: ${'a'.repeat(256_001)}`, 'stream_protocol_invalid'],
  ]
  for (const [name, text, code] of cases) {
    await t.test(name, async () => {
      const { streamConversationMessage } = load(async () => response(text))
      await assert.rejects(
        streamConversationMessage('/stream', REQUEST, { onEvent: () => {} }),
        (error) => error.code === code,
      )
    })
  }
})

test('caller cancellation and deadline stop the fetch stream', async (t) => {
  for (const timed of [false, true]) {
    await t.test(timed ? 'deadline' : 'caller cancellation', async () => {
      const controller = new globalThis.AbortController()
      let fetchSignal
      const { streamConversationMessage } = load(async (_url, options) => {
        fetchSignal = options.signal
        return new globalThis.Response(
          new ReadableStream({
            start(stream) {
              options.signal.addEventListener(
                'abort',
                () => stream.error(new globalThis.DOMException('aborted', 'AbortError')),
                { once: true },
              )
            },
          }),
          { headers: { 'Content-Type': 'text/event-stream' } },
        )
      })
      const pending = streamConversationMessage('/stream', REQUEST, {
        signal: controller.signal,
        timeoutMs: timed ? 5 : 150_000,
        onEvent: () => {},
      })
      if (!timed) controller.abort()
      await assert.rejects(pending, (error) => error.name === 'AbortError')
      assert.equal(fetchSignal.aborted, true)
    })
  }
})

test('published maximum budget accepts six tools and over 1880 valid events in one network chunk', async () => {
  const traces = []
  const frames = [accepted()]
  for (let index = 0; index < 6; index += 1) {
    frames.push(frame('phase', { phase: 'model', message: '正在规划', call: index + 1 }))
    frames.push(frame('tool_started', { index, name: 'read_hot_news' }))
    frames.push(
      frame('tool_finished', {
        index,
        name: 'read_hot_news',
        status: 'completed',
        attempts: 2,
        error_code: null,
      }),
    )
    traces.push({
      name: 'read_hot_news',
      status: 'completed',
      attempts: 2,
      error_code: null,
      arguments: {},
      result: {},
    })
  }
  const answer = 'a'.repeat(30_000)
  for (let index = 0; index < 1_875; index += 1)
    frames.push(delta(index, answer.slice(index * 16, (index + 1) * 16)))
  const final = { ...TURN, assistant_content: answer, tools: traces }
  frames.push(frame('done', { turn: final }))
  const events = []
  const { streamConversationMessage } = load(async () => response(frames.join('')))
  assert.deepEqual(
    await streamConversationMessage('/stream', REQUEST, { onEvent: (event) => events.push(event) }),
    final,
  )
  assert.ok(events.length >= 1_880)
  assert.equal(events.filter((event) => event.type === 'tool_finished').length, 6)
})

test('a seventh tool or third attempt exceeds the published finite budget', async (t) => {
  const trace = {
    name: 'read_hot_news',
    status: 'completed',
    attempts: 1,
    error_code: null,
    arguments: {},
    result: {},
  }
  const cases = {
    seventh_terminal_tool:
      accepted() +
      frame('done', { turn: { ...TURN, tools: Array.from({ length: 7 }, () => trace) } }),
    seventh_started_tool:
      accepted() +
      Array.from({ length: 7 }, (_, index) =>
        frame('tool_started', { index, name: 'read_hot_news' }),
      ).join(''),
    third_finished_attempt:
      accepted() +
      frame('tool_started', { index: 0, name: 'read_hot_news' }) +
      frame('tool_finished', {
        index: 0,
        name: 'read_hot_news',
        status: 'completed',
        attempts: 3,
        error_code: null,
      }),
    third_terminal_attempt:
      accepted() + frame('done', { turn: { ...TURN, tools: [{ ...trace, attempts: 3 }] } }),
  }
  for (const [name, text] of Object.entries(cases)) {
    await t.test(name, async () => {
      const { streamConversationMessage } = load(async () => response(text))
      await assert.rejects(
        streamConversationMessage('/stream', REQUEST, { onEvent: () => {} }),
        (error) => error.code === 'stream_protocol_invalid',
      )
    })
  }
})
