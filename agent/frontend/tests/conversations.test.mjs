import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { setImmediate } from 'node:timers/promises'

import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'

const apiSource = readFileSync(new URL('../src/api/conversations.ts', import.meta.url), 'utf8')
const viewSource = readFileSync(new URL('../src/views/ChatView.vue', import.meta.url), 'utf8')

class ApiError extends Error {
  constructor(status, detail = 'test error') {
    super(detail)
    this.status = status
  }
}

function loadModule(source, imports) {
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  })
  const exports = {}
  new Function('require', 'exports', outputText)((name) => {
    assert.ok(Object.hasOwn(imports, name), `Unexpected import: ${name}`)
    return imports[name]
  }, exports)
  return exports
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((accept, decline) => {
    resolve = accept
    reject = decline
  })
  return { promise, resolve, reject }
}

const conversation = (id) => ({
  id,
  title: id,
  created_at: '2026-10-04T00:00:00Z',
  updated_at: '2026-10-04T00:00:00Z',
})
const turn = (requestId, content = '回答', status = 'completed') => ({
  id: `turn-${requestId}`,
  request_id: requestId,
  user_content: '问题',
  assistant_content: status === 'completed' ? content : null,
  status,
  tools: [],
  model_request_ids: [],
  error_code: status === 'failed' ? 'model_failed' : null,
  created_at: '2026-10-04T00:00:00Z',
  completed_at: status === 'processing' ? null : '2026-10-04T00:00:01Z',
})

async function settle() {
  await setImmediate()
  await vue.nextTick()
}

async function harness(t, overrides = {}, selectedId = 'a', controls = {}) {
  const route = vue.reactive({ query: selectedId ? { conversation: selectedId } : {} })
  const router = {
    replace: async ({ query }) => {
      route.query = query
    },
  }
  const lifecycle = { mounted: [], unmounted: [] }
  const confirmations = []
  const notices = []
  const api = {
    runtime: async () => ({ model_provider: 'local', model_route: 'local-test', query_enabled: false, query_scope: null }),
    list: async () => ({ items: [conversation('a'), conversation('b')] }),
    create: async () => conversation('new-conversation'),
    detail: async (id) => ({ conversation: conversation(id), turns: [] }),
    remove: async () => undefined,
    restore: async (id) => conversation(id),
    sendStream: async (_id, request) => turn(request.request_id),
    ...overrides,
  }
  const { descriptor } = parse(viewSource)
  const compiled = compileScript(descriptor, { id: 'chat-test' })
    .content.replaceAll('import.meta.env.DEV', 'false')
    .replaceAll('import.meta.env.VITE_LOCAL_SIMULATION', "'0'")
  const component = loadModule(compiled, {
    vue: {
      ...vue,
      onMounted: (callback) => lifecycle.mounted.push(callback),
      onBeforeUnmount: (callback) => lifecycle.unmounted.push(callback),
    },
    'vue-router': { useRoute: () => route, useRouter: () => router },
    '@/api/http': { ApiError },
    '@/api/conversations': { conversationsApi: api },
    '@/utils/errors': { describeError: (cause) => cause.message },
    '@/utils/format': { formatDateTime: (value) => value },
    '@/stores/confirm': { useConfirmStore: () => ({ ask: async (options) => {
      confirmations.push(options)
      return controls.confirm ? controls.confirm(options) : true
    } }) },
    '@/stores/toast': { useToastStore: () => ({
      ok: (text) => notices.push({ kind: 'ok', text }),
      fail: (text) => notices.push({ kind: 'error', text }),
    }) },
    '@/components/conversation/AnalysisDemoPanel.vue': { default: {} },
    '@/components/conversation/AnalysisResultCard.vue': { default: {} },
  }).default
  let scope = vue.effectScope()
  const state = scope.run(() => component.setup({}, { expose: () => {} }))
  t.after(() => {
    lifecycle.unmounted.forEach((callback) => callback())
    scope.stop()
  })
  await settle()
  return {
    api,
    route,
    state,
    confirmations,
    notices,
    unmount: () => {
      lifecycle.unmounted.forEach((callback) => callback())
      scope.stop()
    },
    remount: async () => {
      lifecycle.unmounted.length = 0
      lifecycle.mounted.length = 0
      scope = vue.effectScope()
      const restored = scope.run(() => component.setup({}, { expose: () => {} }))
      await settle()
      return restored
    },
    mount: async () => {
      lifecycle.mounted.forEach((callback) => callback())
      await settle()
    },
  }
}

test('model mode comes from authenticated backend runtime, not the frontend simulation flag', async (t) => {
  const value = { model_provider: 'openai_compatible', model_route: 'approved-model',
    prompt_version: 'compatible-conversation-v2', query_enabled: true,
    query_scope: { scenario_id: 'news-ranking', window_start: '2026-10-03T00:00:00+08:00', window_end: '2026-10-03T01:00:00+08:00' } }
  const { state, mount } = await harness(t, { runtime: async () => value })
  await mount()
  assert.equal(state.runtimeInfo.value.model_provider, 'openai_compatible')
  assert.equal(state.runtimeInfo.value.query_enabled, true)
  assert.equal(state.runtimeError.value, null)
  assert.ok(viewSource.includes("runtimeInfo?.model_provider === 'local'"))
  assert.ok(!viewSource.includes('localSimulationEnabled'))
})

test('runtime fetch failure stays unknown instead of claiming a real model connection', async (t) => {
  const { state, mount } = await harness(t, { runtime: async () => { throw new Error('private failure') } })
  await mount()
  assert.equal(state.runtimeInfo.value, null)
  assert.match(state.runtimeError.value, /无法确认/)
  assert.ok(!state.runtimeError.value.includes('private failure'))
})

for (const example of ['看看热点新闻', '今日热点']) {
  test(`natural example ${example} fills only the draft until the user sends`, async (t) => {
    let created = 0
    const sent = []
    const { state, route } = await harness(t, {
      create: async () => { created++; return conversation('new-conversation') },
      sendStream: async (_id, request) => { sent.push(request); return turn(request.request_id) },
    }, '')
    assert.ok(state.examples.includes(example))
    state.useExample(example)
    await settle()
    assert.equal(state.draft.value, example)
    assert.equal(route.query.conversation, undefined)
    assert.equal(created, 0)
    assert.equal(sent.length, 0)

    await state.sendMessage()
    assert.equal(created, 1)
    assert.equal(sent.length, 1)
    assert.equal(sent[0].content, example)
  })
}

test('natural examples keep the existing questions and respect message and deletion locks', async (t) => {
  const confirmation = deferred()
  let sent = 0
  let removed = 0
  const { state } = await harness(t, {
    sendStream: async (_id, request) => { sent++; return turn(request.request_id) },
    remove: async () => { removed++ },
  }, 'a', { confirm: () => confirmation.promise })
  assert.ok(state.examples.includes('查询点击率最高的前5条视频新闻并分析原因'))
  assert.ok(state.examples.includes('解释第一条新闻'))
  assert.ok(state.examples.includes('检索人工智能相关新闻'))
  state.draft.value = '保留草稿'
  state.turns.value = [turn('processing', '', 'processing')]
  state.useExample('看看热点新闻')
  assert.equal(state.draft.value, '保留草稿')
  state.turns.value = []
  const deleting = state.deleteConversation(conversation('a'))
  state.useExample('今日热点')
  await state.sendMessage()
  assert.equal(state.draft.value, '保留草稿')
  assert.equal(sent, 0)
  assert.equal(removed, 0)
  confirmation.resolve(false)
  await deleting
})

test('analysis examples only fill a draft and preparation locks sends and conversation creation', async (t) => {
  let created = 0
  let sent = 0
  const { state } = await harness(t, {
    create: async () => { created++; return conversation('new') },
    sendStream: async (_id, request) => { sent++; return turn(request.request_id) },
  })
  state.useExample('比较当前热点点击量与基线')
  assert.equal(state.draft.value, '比较当前热点点击量与基线')
  assert.equal(sent, 0)
  state.demoPreparing.value = true
  state.useExample('统计两个窗口点击率趋势')
  await state.sendMessage()
  await state.createConversation()
  assert.equal(state.draft.value, '比较当前热点点击量与基线')
  assert.equal(created, 0)
  assert.equal(sent, 0)
})

test('prepared conversation routing respects the origin and load guards', async (t) => {
  const { state, route, unmount } = await harness(t)
  state.onDemoPrepared('demo', 'different')
  await settle()
  assert.equal(route.query.conversation, 'a')
  state.onDemoPrepared('demo', 'a')
  await settle()
  assert.equal(route.query.conversation, 'demo')
  assert.equal(state.conversation.value.id, 'demo')
  unmount()
  state.onDemoPrepared('late', 'demo')
  await settle()
  assert.equal(route.query.conversation, 'demo')
})

test('demo preparation passes cancellation to existing conversation transport without identity headers', async () => {
  const calls = []
  const api = loadModule(apiSource, {
    '@/api/conversationStream': { streamConversationMessage: () => {} },
    '@/api/http': { request: async (...args) => { calls.push(args); return {} } },
  }).conversationsApi
  const controller = new AbortController()
  await api.create('演示', { signal: controller.signal })
  await api.send('demo', { request_id: 'public-request', content: '读取热点运行 public-id' }, { signal: controller.signal })
  assert.equal(calls[0][1].signal, controller.signal)
  assert.equal(calls[1][1].signal, controller.signal)
  assert.ok(calls.every((call) => !Object.hasOwn(call[1], 'headers')))
})

test('conversation API reuses request authentication and allows 120 seconds for messages', async () => {
  const calls = []
  const api = loadModule(apiSource, {
    '@/api/conversationStream': { streamConversationMessage: () => {} },
    '@/api/http': {
      request: async (...args) => {
        calls.push(args)
        return {}
      },
    },
  }).conversationsApi
  await api.list()
  await api.create()
  await api.detail('a/b')
  await api.send('a/b', { request_id: 'fixed-id', content: '问题' })
  assert.deepEqual(calls, [
    ['/api/v1/conversations'],
    ['/api/v1/conversations', { method: 'POST', body: {} }],
    ['/api/v1/conversations/a%2Fb'],
    [
      '/api/v1/conversations/a%2Fb/messages',
      { method: 'POST', body: { request_id: 'fixed-id', content: '问题' }, timeoutMs: 120_000 },
    ],
  ])
})

test('duplicate submits are locked and an uncertain network retry keeps its request ID and content', async (t) => {
  const first = deferred()
  const calls = []
  const { state } = await harness(t, {
    sendStream: async (id, request) => {
      calls.push({ id, ...request })
      return calls.length === 1 ? first.promise : turn(request.request_id)
    },
  })
  state.draft.value = '  查看最近热点  '
  const sending = state.sendMessage()
  await state.sendMessage()
  assert.equal(calls.length, 1)
  assert.equal(state.composerLocked.value, true)
  first.reject(new TypeError('network failed'))
  await sending
  assert.equal(state.pending.value.state, 'retryable')
  assert.equal(state.pending.value.request.content, '查看最近热点')
  await state.retryMessage()
  assert.deepEqual(calls[1], calls[0])
  assert.equal(state.pending.value, undefined)
  assert.equal(state.turns.value.length, 1)
})

test('429 and 409 explain retry behavior while retaining the original request', async (t) => {
  for (const status of [429, 409]) {
    await t.test(String(status), async (subtest) => {
      const { state } = await harness(subtest, {
        sendStream: async () => {
          throw new ApiError(status)
        },
      })
      state.draft.value = '查看最近热点'
      await state.sendMessage()
      const original = state.pending.value.request.request_id
      assert.match(state.pending.value.error, status === 429 ? /过于频繁/ : /刷新历史/)
      await state.retryMessage()
      assert.equal(state.pending.value.request.request_id, original)
    })
  }
})

test('a late message response cannot overwrite a different selected conversation', async (t) => {
  const reply = deferred()
  const bTurn = turn('b-history', 'B 的历史')
  const { state } = await harness(t, {
    sendStream: () => reply.promise,
    detail: async (id) => ({ conversation: conversation(id), turns: id === 'b' ? [bTurn] : [] }),
  })
  state.draft.value = 'A 的问题'
  const sending = state.sendMessage()
  await state.selectConversation('b')
  await settle()
  reply.resolve(turn('a-reply', 'A 的回答'))
  await sending
  assert.equal(state.conversation.value.id, 'b')
  assert.deepEqual(state.turns.value, [bTurn])
  assert.equal(state.pending.value, undefined)
})

test('out-of-order history responses respect the current route query', async (t) => {
  const first = deferred()
  const { state } = await harness(t, {
    detail: async (id) =>
      id === 'a' ? first.promise : { conversation: conversation(id), turns: [turn('b')] },
  })
  await state.selectConversation('b')
  await settle()
  first.resolve({ conversation: conversation('a'), turns: [turn('a')] })
  await settle()
  assert.equal(state.selectedConversationId.value, 'b')
  assert.equal(state.conversation.value.id, 'b')
  assert.equal(state.turns.value[0].request_id, 'b')
  assert.equal(state.detailLoading.value, false)
})

test('refresh recovers the original result and a late response does not erase a newer pending request', async (t) => {
  const first = deferred()
  const second = deferred()
  const sent = []
  const stored = []
  const { state } = await harness(t, {
    detail: async (id) => ({ conversation: conversation(id), turns: [...stored] }),
    sendStream: (_id, request) => {
      sent.push({ ...request })
      return sent.length === 1 ? first.promise : second.promise
    },
  })
  state.draft.value = '第一条'
  const firstSend = state.sendMessage()
  stored.push(turn(sent[0].request_id))
  await state.loadDetail('a')
  assert.equal(state.pending.value, undefined)
  state.draft.value = '第二条'
  const secondSend = state.sendMessage()
  first.resolve(stored[0])
  await firstSend
  assert.equal(state.pending.value.request.request_id, sent[1].request_id)
  second.resolve(turn(sent[1].request_id))
  await secondSend
})

test('a failed business turn remains in history and an explicit new message receives a new request ID', async (t) => {
  const sent = []
  const { state } = await harness(t, {
    sendStream: async (_id, request) => {
      sent.push(request)
      return turn(request.request_id, '', 'failed')
    },
  })
  state.draft.value = '问题'
  await state.sendMessage()
  assert.equal(state.turns.value[0].status, 'failed')
  assert.equal(state.composerLocked.value, false)
  state.draft.value = '重新提问'
  await state.sendMessage()
  assert.notEqual(sent[0].request_id, sent[1].request_id)
  assert.equal(state.turns.value.length, 2)
})

test('route history recovers processing state and prevents another send', async (t) => {
  let sends = 0
  const { state } = await harness(
    t,
    {
      detail: async (id) => ({
        conversation: conversation(id),
        turns: [turn('processing', '', 'processing')],
      }),
      sendStream: async () => {
        sends += 1
      },
    },
    'restored',
  )
  assert.equal(state.selectedConversationId.value, 'restored')
  assert.equal(state.serverProcessing.value, true)
  state.draft.value = '重复问题'
  await state.sendMessage()
  assert.equal(sends, 0)
})

test('a stale processing response cannot regress a terminal turn recovered from history', async (t) => {
  const reply = deferred()
  let saved = []
  const { state } = await harness(t, {
    detail: async (id) => ({ conversation: conversation(id), turns: saved }),
    sendStream: () => reply.promise,
  })
  state.draft.value = '问题'
  const sending = state.sendMessage()
  const id = state.pending.value.request.request_id
  saved = [turn(id)]
  await state.loadDetail('a')
  reply.resolve(turn(id, '', 'processing'))
  await sending
  assert.equal(state.turns.value[0].status, 'completed')
  assert.equal(state.composerLocked.value, false)
})

test('an arbitrary route ID cannot expose dictionary prototype members as pending messages', async (t) => {
  const { state } = await harness(t, {}, '__proto__')
  assert.equal(state.pending.value, undefined)
  state.draft.value = '问题'
  await state.sendMessage()
  assert.equal(state.turns.value.length, 1)
})

test('a first message creates a conversation and restores the route without storing messages', async (t) => {
  const calls = []
  const { state, route } = await harness(
    t,
    {
      sendStream: async (id, request) => {
        calls.push({ id, ...request })
        return turn(request.request_id)
      },
    },
    null,
  )
  state.draft.value = '开始对话'
  await state.sendMessage()
  await settle()
  assert.equal(route.query.conversation, 'new-conversation')
  assert.equal(calls[0].id, 'new-conversation')
  assert.equal(calls[0].content, '开始对话')
  assert.equal(state.draft.value, '')
  assert.equal(viewSource.includes('localStorage'), false)
  assert.equal(viewSource.includes('v-html'), false)
})

test('phase, tool progress and safe answer text appear before done without committing a turn', async (t) => {
  const complete = deferred()
  const malicious = '<img src=x onerror="alert(1)">中文🙂'
  const { state } = await harness(t, {
    sendStream: (_id, request, options) => {
      options.onEvent({
        type: 'accepted',
        data: {
          turn_id: 'accepted-turn',
          request_id: request.request_id,
          replayed: false,
          status: 'processing',
        },
      })
      options.onEvent({ type: 'phase', data: { phase: 'model', message: '模型正在规划' } })
      options.onEvent({ type: 'tool_started', data: { index: 0, name: 'read_hot_news' } })
      options.onEvent({
        type: 'tool_finished',
        data: {
          index: 0,
          name: 'read_hot_news',
          status: 'completed',
          attempts: 1,
          error_code: null,
        },
      })
      options.onEvent({
        type: 'answer_delta',
        data: {
          turn_id: 'accepted-turn',
          request_id: request.request_id,
          index: 0,
          text: malicious,
        },
      })
      return complete.promise
    },
  })
  state.draft.value = '问题'
  const sending = state.sendMessage()
  assert.equal(state.pending.value.partialAnswer, malicious)
  assert.equal(state.pending.value.tools[0].status, 'completed')
  assert.equal(state.turns.value.length, 0)
  assert.equal(state.composerLocked.value, true)
  complete.resolve(turn(state.pending.value.request.request_id, malicious))
  await sending
  assert.equal(state.turns.value[0].assistant_content, malicious)
  assert.equal(state.pending.value, undefined)
  assert.equal(viewSource.includes('v-html'), false)
})

test('unmount cancels the stream and SPA remount retains the same uncertain request ID', async (t) => {
  const calls = []
  let firstSignal
  const value = await harness(t, {
    sendStream: (_id, request, options) => {
      calls.push({ ...request })
      if (calls.length > 1) return Promise.resolve(turn(request.request_id))
      firstSignal = options.signal
      return new Promise((_resolve, reject) =>
        options.signal.addEventListener('abort', () => reject(new Error('aborted')), {
          once: true,
        }),
      )
    },
  })
  value.state.draft.value = '保留原请求'
  const sending = value.state.sendMessage()
  value.unmount()
  await sending
  assert.equal(firstSignal.aborted, true)
  const restored = await value.remount()
  assert.equal(restored.pending.value.state, 'retryable')
  assert.equal(restored.pending.value.request.request_id, calls[0].request_id)
  await restored.retryMessage()
  assert.deepEqual(calls[1], calls[0])
})

test('late stream deltas and done cannot overwrite a turn already recovered by GET', async (t) => {
  const completion = deferred()
  let emit
  let saved = []
  const { state } = await harness(t, {
    detail: async (id) => ({ conversation: conversation(id), turns: saved }),
    sendStream: (_id, _request, options) => {
      emit = options.onEvent
      return completion.promise
    },
  })
  state.draft.value = '问题'
  const sending = state.sendMessage()
  const requestId = state.pending.value.request.request_id
  saved = [turn(requestId, '持久终态')]
  await state.loadDetail('a')
  emit({
    type: 'answer_delta',
    data: { turn_id: 'late', request_id: requestId, index: 0, text: '迟到内容' },
  })
  completion.resolve(turn(requestId, '迟到内容'))
  await sending
  assert.equal(state.pending.value, undefined)
  assert.equal(state.turns.value[0].assistant_content, '持久终态')
})

test('terminal replay progress is labeled as restoring a saved reply', async (t) => {
  const completion = deferred()
  const { state } = await harness(t, {
    sendStream: (_id, request, options) => {
      options.onEvent({
        type: 'accepted',
        data: {
          turn_id: 'saved',
          request_id: request.request_id,
          replayed: true,
          status: 'completed',
        },
      })
      return completion.promise
    },
  })
  state.draft.value = '你好'
  const sending = state.sendMessage()
  assert.equal(state.pending.value.replayed, true)
  assert.match(state.pending.value.phase, /恢复已保存/)
  completion.resolve(turn(state.pending.value.request.request_id))
  await sending
})

test('delete and restore encode IDs and reuse gateway authentication without a request body', async () => {
  const calls = []
  const api = loadModule(apiSource, {
    '@/api/conversationStream': { streamConversationMessage: () => {} },
    '@/api/http': { request: async (...args) => { calls.push(args) } },
  }).conversationsApi
  await api.remove('a/b')
  await api.restore('a/b')
  assert.deepEqual(calls, [
    ['/api/v1/conversations/a%2Fb', { method: 'DELETE' }],
    ['/api/v1/conversations/a%2Fb/restore', { method: 'POST' }],
  ])
})

test('delete requires confirmation and updates the page only after the server succeeds', async (t) => {
  const response = deferred()
  let removed = 0
  const { state, route, mount, confirmations } = await harness(t, {
    remove: () => { removed++; return response.promise },
    detail: async (id) => ({ conversation: conversation(id), turns: [turn(`${id}-history`)] }),
  })
  await mount()
  state.draft.value = 'A 的草稿'
  const deleting = state.deleteConversation(conversation('a'))
  await settle()
  assert.equal(removed, 1)
  assert.match(confirmations[0].description, /历史记录保留/)
  assert.match(confirmations[0].description, /热点运行不会删除/)
  assert.equal(state.conversation.value.id, 'a')
  assert.equal(state.draft.value, 'A 的草稿')
  assert.equal(state.conversations.value.length, 2)
  assert.equal(state.composerLocked.value, true)
  response.resolve()
  await deleting
  await settle()
  assert.equal(route.query.conversation, 'b')
  assert.equal(state.conversation.value.id, 'b')
  assert.deepEqual(state.conversations.value.map((item) => item.id), ['b'])
  assert.equal(Object.hasOwn(state.drafts, 'a'), false)
  assert.equal(state.outbox.has('a'), false)
  assert.equal(state.lastDeletedConversation.value.id, 'a')
})

test('cancelling delete leaves list, selection and draft unchanged', async (t) => {
  let removed = 0
  const { state, route, mount } = await harness(t, {
    remove: async () => { removed++ },
  }, 'a', { confirm: async () => false })
  await mount()
  state.draft.value = '保留草稿'
  await state.deleteConversation(conversation('a'))
  assert.equal(removed, 0)
  assert.equal(route.query.conversation, 'a')
  assert.equal(state.draft.value, '保留草稿')
  assert.equal(state.lastDeletedConversation.value, null)
  assert.equal(state.conversations.value.length, 2)
  assert.equal(state.deletingConversationId.value, null)
})

test('409 and uncertain transport errors keep local state and explain how to confirm the result', async (t) => {
  for (const cause of [new ApiError(409, 'conversation has a processing turn'), new TypeError('connection lost')]) {
    await t.test(cause.constructor.name, async (subtest) => {
      const { state, route, mount, notices } = await harness(subtest, {
        remove: async () => { throw cause },
      })
      await mount()
      state.draft.value = '仍在编辑'
      await state.deleteConversation(conversation('a'))
      assert.equal(route.query.conversation, 'a')
      assert.equal(state.draft.value, '仍在编辑')
      assert.equal(state.deletedConversationIds.size, 0)
      assert.equal(state.lastDeletedConversation.value, null)
      assert.equal(state.conversations.value.length, 2)
      assert.match(notices.at(-1).text, cause.status === 409 ? /正在处理.*暂时无法删除/ : /未确认.*刷新会话列表确认/)
    })
  }
})

test('deleting the last current conversation clears its route, history, retryable outbox and drafts', async (t) => {
  const { state, route, mount } = await harness(t, {
    list: async () => ({ items: [conversation('a')] }),
    sendStream: async () => { throw new TypeError('connection lost') },
  })
  await mount()
  state.draft.value = '尚未确认的问题'
  await state.sendMessage()
  assert.equal(state.pending.value.state, 'retryable')
  state.draft.value = '残留草稿'
  await state.deleteConversation(conversation('a'))
  await settle()
  assert.equal(Object.hasOwn(route.query, 'conversation'), false)
  assert.equal(state.selectedConversationId.value, null)
  assert.equal(state.conversation.value, null)
  assert.deepEqual(state.turns.value, [])
  assert.equal(state.pending.value, undefined)
  assert.equal(state.draft.value, '')
  assert.equal(state.pollTimer, null)
  assert.equal(state.conversations.value.length, 0)
})

test('deleting another conversation preserves the current history, route and draft', async (t) => {
  const saved = turn('a-history')
  const { state, route, mount } = await harness(t, {
    detail: async (id) => ({ conversation: conversation(id), turns: [saved] }),
  })
  await mount()
  state.draft.value = '正在编辑 A'
  await state.deleteConversation(conversation('b'))
  assert.equal(route.query.conversation, 'a')
  assert.equal(state.conversation.value.id, 'a')
  assert.deepEqual(state.turns.value, [saved])
  assert.equal(state.draft.value, '正在编辑 A')
  assert.deepEqual(state.conversations.value.map((item) => item.id), ['a'])
})

test('late list, GET, events and navigation cannot resurrect a successfully deleted conversation', async (t) => {
  const oldList = deferred()
  const oldDetail = deferred()
  let lists = 0
  let details = 0
  let emit
  const { state, route, mount } = await harness(t, {
    list: async () => ++lists === 2 ? oldList.promise : { items: [conversation('a'), conversation('b')] },
    detail: async (id) => {
      details++
      return id === 'a' && details > 1 ? oldDetail.promise : { conversation: conversation(id), turns: [] }
    },
    sendStream: async (_id, _body, options) => { emit = options.onEvent; throw new TypeError('connection lost') },
  })
  await mount()
  state.draft.value = '问题'
  await state.sendMessage()
  const entry = state.pending.value
  const listing = state.loadConversations()
  const loading = state.loadDetail('a')
  await state.deleteConversation(conversation('a'))
  await settle()
  emit({ type: 'answer_delta', data: { turn_id: 'late', request_id: entry.request.request_id, index: 0, text: '迟到内容' } })
  oldDetail.resolve({ conversation: conversation('a'), turns: [turn(entry.request.request_id, '旧答复')] })
  oldList.resolve({ items: [conversation('a'), conversation('b')] })
  await Promise.all([listing, loading])
  await settle()
  assert.equal(route.query.conversation, 'b')
  assert.equal(state.conversation.value.id, 'b')
  assert.deepEqual(state.turns.value, [])
  assert.deepEqual(state.conversations.value.map((item) => item.id), ['b'])
  assert.equal(state.outbox.has('a'), false)
  assert.equal(state.conversationsLoading.value, false)
  // 即使一个较新的列表仍来自删除前的服务端快照，也保留本页 tombstone。
  await state.loadConversations()
  assert.deepEqual(state.conversations.value.map((item) => item.id), ['b'])
  const callsBeforeDeletedRoute = details
  route.query = { conversation: 'a' }
  await settle()
  assert.equal(route.query.conversation, undefined)
  assert.equal(details, callsBeforeDeletedRoute)
})

test('a pending confirmation prevents duplicate mutations and rechecks busy state before DELETE', async (t) => {
  const confirmation = deferred()
  let removed = 0
  let created = 0
  const { state, mount, confirmations } = await harness(t, {
    remove: async () => { removed++ },
    create: async () => { created++; return conversation('new') },
  }, 'a', { confirm: () => confirmation.promise })
  await mount()
  const deleting = state.deleteConversation(conversation('a'))
  await state.deleteConversation(conversation('b'))
  await state.undoConversationDeletion()
  await state.createConversation()
  state.draft.value = '确认期间不能发送'
  await state.sendMessage()
  assert.equal(confirmations.length, 1)
  assert.equal(created, 0)
  assert.equal(state.pending.value, undefined)
  state.demoPreparing.value = true
  confirmation.resolve(true)
  await deleting
  assert.equal(removed, 0)
  assert.equal(state.deletingConversationId.value, null)
})

test('local streams lock only their own deletion while server processing, create and preparation also lock', async (t) => {
  const completion = deferred()
  let removed = 0
  const { state, mount } = await harness(t, {
    remove: async () => { removed++ },
    sendStream: (_id, request) => completion.promise.then(() => turn(request.request_id)),
  })
  await mount()
  state.draft.value = '进行中的请求'
  const sending = state.sendMessage()
  await state.selectConversation('b')
  await settle()
  assert.equal(state.deletionDisabled('a'), true)
  assert.equal(state.deletionDisabled('b'), false)
  await state.deleteConversation(conversation('a'))
  assert.equal(removed, 0)
  state.turns.value = [turn('server', '', 'processing')]
  await state.deleteConversation(conversation('b'))
  state.turns.value = []
  state.demoPreparing.value = true
  await state.deleteConversation(conversation('b'))
  state.demoPreparing.value = false
  state.creating.value = true
  await state.deleteConversation(conversation('b'))
  state.creating.value = false
  assert.equal(removed, 0)
  completion.resolve()
  await sending
})

test('undo restores the hidden conversation without replacing an existing current conversation or its draft', async (t) => {
  const restoring = deferred()
  const oldList = deferred()
  let lists = 0
  const { state, route, mount } = await harness(t, {
    restore: () => restoring.promise,
    list: async () => ++lists === 2 ? oldList.promise : { items: [conversation('a'), conversation('b')] },
  })
  await mount()
  await state.deleteConversation(conversation('b'))
  state.draft.value = 'A 的草稿'
  const listing = state.loadConversations()
  const undo = state.undoConversationDeletion()
  assert.equal(state.deletionDisabled('a'), true)
  assert.equal(state.deletedConversationIds.has('b'), true)
  assert.equal(state.lastDeletedConversation.value.id, 'b')
  restoring.resolve(conversation('b'))
  await undo
  oldList.resolve({ items: [conversation('a')] })
  await listing
  assert.equal(state.deletedConversationIds.has('b'), false)
  assert.equal(state.lastDeletedConversation.value, null)
  assert.equal(route.query.conversation, 'a')
  assert.equal(state.draft.value, 'A 的草稿')
  assert.equal(state.conversations.value.some((item) => item.id === 'b'), true)
  assert.equal(state.conversationsLoading.value, false)
})

test('undo failure keeps its retry action; successful undo opens the conversation when the list was empty', async (t) => {
  let restores = 0
  const saved = turn('saved-history')
  const { state, route, mount, notices } = await harness(t, {
    list: async () => ({ items: [conversation('a')] }),
    detail: async (id) => ({ conversation: conversation(id), turns: [saved] }),
    restore: async (id) => {
      if (++restores === 1) throw new TypeError('connection lost')
      return conversation(id)
    },
  })
  await mount()
  await state.deleteConversation(conversation('a'))
  await state.undoConversationDeletion()
  assert.match(notices.at(-1).text, /撤销删除失败/)
  assert.equal(state.lastDeletedConversation.value.id, 'a')
  assert.equal(state.deletedConversationIds.has('a'), true)
  await state.undoConversationDeletion()
  await settle()
  assert.equal(route.query.conversation, 'a')
  assert.deepEqual(state.turns.value, [saved])
  assert.equal(state.lastDeletedConversation.value, null)
})

test('conversation selection and deletion use sibling buttons, preserving accessible independent actions', () => {
  const { descriptor } = parse(viewSource)
  let deletionButton
  function visit(node, buttonAncestor = false) {
    if (node.type === 1) {
      if (node.tag === 'button') {
        assert.equal(buttonAncestor, false, 'button cannot be nested inside another button')
        if (node.props.some((prop) => prop.name === 'class' && prop.value?.content.includes('na-chat__delete'))) deletionButton = node
      }
      for (const child of node.children) visit(child, buttonAncestor || node.tag === 'button')
    } else if (node.children) for (const child of node.children) visit(child, buttonAncestor)
  }
  visit(descriptor.template.ast)
  assert.ok(deletionButton)
  assert.ok(deletionButton.props.some((prop) => prop.name === 'bind' && prop.arg?.content === 'aria-label'))
})
