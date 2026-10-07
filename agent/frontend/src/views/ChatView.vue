<script lang="ts">
import { reactive as reactiveOutbox } from 'vue'
import type { SendConversationMessage } from '@/api/conversations'

interface PendingMessage {
  request: SendConversationMessage
  state: 'sending' | 'retryable'
  error: string | null
  phase: string
  partialAnswer: string
  turnId: string | null
  replayed: boolean
  tools: Array<{
    index: number
    name: string
    status: 'running' | 'completed' | 'failed' | 'denied'
    attempts: number | null
    error_code: string | null
  }>
}

// 仅驻留当前 SPA 的内存：离开页面取消流后仍可用原 UUID 重试，不落浏览器存储。
const conversationOutbox = reactiveOutbox(new Map<string, PendingMessage>())
</script>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError } from '@/api/http'
import { conversationsApi } from '@/api/conversations'
import type { Conversation, ConversationTurn, ConversationRuntime } from '@/api/conversations'
import type { ConversationStreamEvent } from '@/api/conversationStream'
import { describeError } from '@/utils/errors'
import { formatDateTime } from '@/utils/format'
import { useConfirmStore } from '@/stores/confirm'
import { useToastStore } from '@/stores/toast'
import AnalysisDemoPanel from '@/components/conversation/AnalysisDemoPanel.vue'
import AnalysisResultCard from '@/components/conversation/AnalysisResultCard.vue'

const route = useRoute()
const router = useRouter()
const dialog = useConfirmStore()
const toast = useToastStore()
const conversations = ref<Conversation[]>([])
const conversationsLoading = ref(false)
const conversationsError = ref<string | null>(null)
const creating = ref(false)
const demoPreparing = ref(false)
const deletingConversationId = ref<string | null>(null)
const restoringConversation = ref(false)
const lastDeletedConversation = ref<Conversation | null>(null)
const deletedConversationIds = reactive(new Set<string>())
const conversationMutationPending = computed(() => !!deletingConversationId.value || restoringConversation.value)
const localAnalysisDemoMode = import.meta.env.DEV && import.meta.env.VITE_LOCAL_SIMULATION === '1'
const conversation = ref<Conversation | null>(null)
const turns = ref<ConversationTurn[]>([])
const detailLoading = ref(false)
const detailError = ref<string | null>(null)
const composerError = ref<string | null>(null)
const messagesElement = ref<HTMLElement | null>(null)
// 只保留当前页面内的草稿和未确认请求，不把消息写进浏览器持久存储。
const drafts = reactive<Record<string, string>>(Object.create(null))
const outbox = conversationOutbox
const activeStreams = new Map<PendingMessage, { id: string; controller: AbortController }>()
const examples = ['看看热点新闻', '今日热点', '查询点击率最高的前5条视频新闻并分析原因', '解释第一条新闻', '检索人工智能相关新闻']
const runtimeInfo = ref<ConversationRuntime | null>(null)
const runtimeError = ref<string | null>(null)
let detailRequestSequence = 0
let listRequestSequence = 0
let pollTimer: ReturnType<typeof setTimeout> | null = null
let disposed = false

const selectedConversationId = computed(() => {
  const value = route.query.conversation
  return typeof value === 'string' && value ? value : null
})
const draftKey = computed(() => selectedConversationId.value ?? 'new')
const draft = computed({
  get: () => drafts[draftKey.value] ?? '',
  set: (value: string) => {
    drafts[draftKey.value] = value
    composerError.value = null
  },
})
const pending = computed(() =>
  selectedConversationId.value ? outbox.get(selectedConversationId.value) : undefined,
)
const serverProcessing = computed(() => turns.value.some((turn) => turn.status === 'processing'))
const composerLocked = computed(
  () =>
    creating.value ||
    demoPreparing.value ||
    (selectedConversationId.value !== null &&
      (deletedConversationIds.has(selectedConversationId.value) || deletingConversationId.value === selectedConversationId.value)) ||
    detailLoading.value ||
    !!detailError.value ||
    !!pending.value ||
    serverProcessing.value,
)
const pendingVisible = computed(
  () =>
    pending.value &&
    !turns.value.some(
      (turn) =>
        turn.request_id === pending.value?.request.request_id && turn.status !== 'processing',
    ),
)
const visibleTurns = computed(() =>
  turns.value.filter(
    (turn) => turn.status !== 'processing' || turn.request_id !== pending.value?.request.request_id,
  ),
)
const draftLength = computed(() => draft.value.trim().length)

function upsertConversation(value: Conversation): void {
  if (deletedConversationIds.has(value.id)) return
  conversations.value = [value, ...conversations.value.filter((item) => item.id !== value.id)].sort(
    (left, right) => right.updated_at.localeCompare(left.updated_at),
  )
}

function clearPoll(): void {
  if (pollTimer !== null) clearTimeout(pollTimer)
  pollTimer = null
}

function schedulePoll(): void {
  clearPoll()
  const id = selectedConversationId.value
  if (
    !id ||
    disposed ||
    deletedConversationIds.has(id) ||
    detailError.value ||
    [...activeStreams.values()].some((stream) => stream.id === id) ||
    (!serverProcessing.value && pending.value?.state !== 'sending')
  ) {
    return
  }
  pollTimer = setTimeout(() => {
    if (selectedConversationId.value === id && !disposed) void loadDetail(id, false)
  }, 3_000)
}

async function selectConversation(id: string): Promise<void> {
  if (disposed || deletedConversationIds.has(id)) return
  await router.replace({ path: '/chat', query: { ...route.query, conversation: id } })
}

function conversationBusy(id: string): boolean {
  return creating.value || demoPreparing.value ||
    outbox.get(id)?.state === 'sending' ||
    [...activeStreams.values()].some((stream) => stream.id === id) ||
    (selectedConversationId.value === id && serverProcessing.value)
}

function deletionDisabled(id: string): boolean {
  return conversationMutationPending.value || deletedConversationIds.has(id) || conversationBusy(id)
}

async function clearConversationRoute(): Promise<void> {
  const query = { ...route.query }
  delete query.conversation
  await router.replace({ path: '/chat', query })
}

async function deleteConversation(item: Conversation): Promise<void> {
  if (disposed || deletionDisabled(item.id)) return
  deletingConversationId.value = item.id
  try {
    const confirmed = await dialog.ask({
      title: '删除这条会话？',
      description: `“${item.title || '新对话'}”将从会话列表隐藏，本页可撤销删除。历史记录保留，关联热点运行不会删除。`,
      confirmText: '删除会话',
      danger: true,
    })
    if (!confirmed || disposed || conversationBusy(item.id)) return
    await conversationsApi.remove(item.id)
    // DELETE 确认成功后再隐藏；旧列表、GET 和流均不能恢复已删除的会话。
    deletedConversationIds.add(item.id)
    ++listRequestSequence
    conversationsLoading.value = false
    outbox.delete(item.id)
    delete drafts[item.id]
    for (const [entry, stream] of activeStreams) {
      if (stream.id === item.id) {
        stream.controller.abort()
        activeStreams.delete(entry)
      }
    }
    if (disposed) return
    conversations.value = conversations.value.filter((value) => value.id !== item.id)
    conversationsError.value = null
    lastDeletedConversation.value = item
    toast.ok('会话已从列表隐藏，历史记录保留；可撤销删除。')
    if (selectedConversationId.value === item.id) {
      ++detailRequestSequence
      clearPoll()
      conversation.value = null
      turns.value = []
      detailLoading.value = false
      detailError.value = null
      composerError.value = null
      drafts.new = ''
      const next = conversations.value[0]
      try {
        if (next) await selectConversation(next.id)
        else await clearConversationRoute()
      } catch {
        composerError.value = '会话已删除，页面切换失败，请刷新页面。'
      }
    }
  } catch (cause) {
    if (!disposed) {
      toast.fail(cause instanceof ApiError && cause.status === 409
        ? '该会话有消息正在处理，暂时无法删除。请刷新历史，等待处理结束后重试。'
        : `删除结果未确认：${describeError(cause)}。请刷新会话列表确认，可使用同一删除操作重试。`)
    }
  } finally {
    deletingConversationId.value = null
  }
}

async function undoConversationDeletion(): Promise<void> {
  const deleted = lastDeletedConversation.value
  if (disposed || !deleted || conversationMutationPending.value || creating.value || demoPreparing.value) return
  restoringConversation.value = true
  try {
    const restored = await conversationsApi.restore(deleted.id)
    if (restored.id !== deleted.id) throw new Error('恢复响应与所选会话不一致')
    if (disposed) return
    ++listRequestSequence
    conversationsLoading.value = false
    deletedConversationIds.delete(deleted.id)
    upsertConversation(restored)
    lastDeletedConversation.value = null
    toast.ok('会话已恢复，已保存的历史记录仍可查看。')
    if (!selectedConversationId.value) {
      try {
        await selectConversation(restored.id)
      } catch {
        composerError.value = '会话已恢复，页面切换失败，请从会话列表打开。'
      }
    }
  } catch (cause) {
    if (!disposed) toast.fail(`撤销删除失败：${describeError(cause)}。可稍后重试。`)
  } finally {
    restoringConversation.value = false
  }
}

async function loadConversations(selectFirst = false): Promise<void> {
  const sequence = ++listRequestSequence
  conversationsLoading.value = true
  conversationsError.value = null
  try {
    const page = await conversationsApi.list()
    if (disposed || sequence !== listRequestSequence) return
    conversations.value = page.items.filter((item) => !deletedConversationIds.has(item.id))
    if (selectFirst && !selectedConversationId.value && !creating.value && !demoPreparing.value && !conversationMutationPending.value && conversations.value[0]) {
      await selectConversation(conversations.value[0].id)
    }
  } catch (cause) {
    if (!disposed && sequence === listRequestSequence)
      conversationsError.value = describeError(cause)
  } finally {
    if (!disposed && sequence === listRequestSequence) conversationsLoading.value = false
  }
}

async function loadDetail(id: string, showLoading = true): Promise<void> {
  if (disposed || deletedConversationIds.has(id)) return
  const sequence = ++detailRequestSequence
  if (showLoading) detailLoading.value = true
  detailError.value = null
  try {
    const value = await conversationsApi.detail(id)
    if (disposed || deletedConversationIds.has(id) || sequence !== detailRequestSequence || selectedConversationId.value !== id)
      return
    conversation.value = value.conversation
    turns.value = value.turns
    upsertConversation(value.conversation)
    const request = outbox.get(id)
    // 刷新发现服务端已完成原请求时，直接恢复结果，不再次提交。
    if (
      request &&
      value.turns.some(
        (turn) => turn.request_id === request.request.request_id && turn.status !== 'processing',
      )
    ) {
      outbox.delete(id)
    }
  } catch (cause) {
    if (!disposed && !deletedConversationIds.has(id) && sequence === detailRequestSequence && selectedConversationId.value === id) {
      detailError.value = describeError(cause)
    }
  } finally {
    if (!disposed && !deletedConversationIds.has(id) && sequence === detailRequestSequence && selectedConversationId.value === id) {
      detailLoading.value = false
      schedulePoll()
    }
  }
}

async function createConversation(): Promise<string | null> {
  if (creating.value || demoPreparing.value || conversationMutationPending.value) return null
  creating.value = true
  composerError.value = null
  try {
    const value = await conversationsApi.create()
    if (disposed) return null
    upsertConversation(value)
    await selectConversation(value.id)
    return value.id
  } catch (cause) {
    if (!disposed) composerError.value = describeError(cause)
    return null
  } finally {
    if (!disposed) creating.value = false
  }
}

function onDemoPrepared(id: string, originContext: string): void {
  if (disposed || conversationMutationPending.value || deletedConversationIds.has(id) ||
      deletedConversationIds.has(originContext) || (selectedConversationId.value ?? '') !== originContext) return
  void selectConversation(id).catch(() => {
    if (!disposed) composerError.value = '演示会话已保存，页面切换失败，请从会话列表打开。'
  })
  void loadConversations(false)
}

function sendError(cause: unknown): string {
  if (cause instanceof ApiError && cause.status === 409) {
    return '该会话有消息正在处理，或原请求与服务端状态冲突。请刷新历史确认结果，再重试原请求。'
  }
  if (cause instanceof ApiError && cause.status === 429) {
    return '请求过于频繁，请稍后重试原请求。'
  }
  return `${describeError(cause)}。请求结果尚未确认；可刷新历史，或使用同一请求编号重试。`
}

async function dispatchMessage(id: string, entry: PendingMessage): Promise<void> {
  if (disposed || deletedConversationIds.has(id) || deletingConversationId.value === id) return
  entry.state = 'sending'
  entry.error = null
  entry.phase = '正在连接 Agent…'
  entry.partialAnswer = ''
  entry.turnId = null
  entry.replayed = false
  entry.tools = []
  const controller = new AbortController()
  activeStreams.set(entry, { id, controller })
  if (selectedConversationId.value === id) schedulePoll()
  try {
    const value = await conversationsApi.sendStream(id, entry.request, {
      signal: controller.signal,
      onEvent: (event) => applyStreamEvent(id, entry, event),
    })
    // GET 已恢复终态或本页已经离开时，迟到流不能覆盖持久历史或新的请求。
    if (disposed || deletedConversationIds.has(id) || outbox.get(id) !== entry) return
    outbox.delete(id)
    // 会话切换后，响应仅属于原会话，不能覆盖当前消息区。
    if (selectedConversationId.value === id) {
      ++detailRequestSequence
      detailLoading.value = false
      detailError.value = null
      const existing = turns.value.findIndex((turn) => turn.request_id === value.request_id)
      if (existing >= 0) {
        // 已从历史恢复的终态不能被更早的 processing 响应退回处理中。
        if (value.status !== 'processing' || turns.value[existing].status === 'processing') {
          turns.value.splice(existing, 1, value)
        }
      } else turns.value.push(value)
      turns.value = turns.value
        .sort((left, right) => left.created_at.localeCompare(right.created_at))
        .slice(-50)
      schedulePoll()
    }
    void loadConversations()
  } catch (cause) {
    if (disposed || deletedConversationIds.has(id) || outbox.get(id) !== entry) return
    // 网络失败并不代表服务端没执行。保留 request_id 与原文，重试不会另起一轮。
    entry.state = 'retryable'
    entry.error = sendError(cause)
    if (selectedConversationId.value === id) schedulePoll()
  } finally {
    activeStreams.delete(entry)
    if (!disposed && selectedConversationId.value === id) schedulePoll()
  }
}

function applyStreamEvent(id: string, entry: PendingMessage, event: ConversationStreamEvent): void {
  if (disposed || deletedConversationIds.has(id) || outbox.get(id) !== entry) return
  if (event.type === 'accepted') {
    entry.turnId = event.data.turn_id
    entry.replayed = event.data.replayed
    entry.phase = event.data.replayed ? '正在恢复已保存的答复…' : '请求已接收，Agent 正在处理…'
  } else if (event.type === 'phase') {
    entry.phase = event.data.message
  } else if (event.type === 'tool_started') {
    entry.tools.push({ ...event.data, status: 'running', attempts: null, error_code: null })
  } else if (event.type === 'tool_finished') {
    const tool = entry.tools.find((item) => item.index === event.data.index)
    if (tool) Object.assign(tool, event.data)
  } else if (event.type === 'answer_delta') {
    entry.partialAnswer += event.data.text
    entry.phase = entry.replayed ? '正在接收已保存的答复…' : '答复已校验并保存，正在接收…'
  }
}

async function sendMessage(): Promise<void> {
  if (composerLocked.value) return
  const content = draft.value.trim()
  if (!content || content.length > 4_000) {
    composerError.value = '请输入 1 至 4000 字的消息。'
    return
  }
  const sourceDraftKey = draftKey.value
  let id = selectedConversationId.value
  if (!id) id = await createConversation()
  if (!id || disposed || outbox.has(id)) return
  drafts[sourceDraftKey] = ''
  drafts[id] = ''
  const entry = reactive<PendingMessage>({
    request: { request_id: crypto.randomUUID(), content },
    state: 'sending',
    error: null,
    phase: '',
    partialAnswer: '',
    turnId: null,
    replayed: false,
    tools: [],
  })
  outbox.set(id, entry)
  await dispatchMessage(id, entry)
}

async function retryMessage(): Promise<void> {
  const id = selectedConversationId.value
  if (!id || !pending.value || pending.value.state !== 'retryable') return
  await dispatchMessage(id, pending.value)
}

function useExample(content: string): void {
  if (!composerLocked.value) draft.value = content
}

function onComposerKeydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.isComposing) {
    event.preventDefault()
    void sendMessage()
  }
}

function statusLabel(status: ConversationTurn['status']): string {
  return { processing: '处理中', completed: '已完成', failed: '处理失败' }[status]
}

function toolStatusLabel(status: ConversationTurn['tools'][number]['status']): string {
  return { completed: '已完成', failed: '失败', denied: '已拒绝' }[status]
}

watch(
  selectedConversationId,
  (id) => {
    ++detailRequestSequence
    clearPoll()
    conversation.value = null
    turns.value = []
    detailLoading.value = false
    detailError.value = null
    composerError.value = null
    if (id && deletedConversationIds.has(id)) {
      void clearConversationRoute().catch(() => {
        if (!disposed) detailError.value = '该会话已删除，请从列表选择其他会话。'
      })
    } else if (id) void loadDetail(id)
  },
  { immediate: true },
)

watch(
  () => [
    selectedConversationId.value,
    turns.value.length,
    pending.value?.request.request_id,
    pending.value?.partialAnswer,
  ],
  async () => {
    await nextTick()
    if (messagesElement.value) messagesElement.value.scrollTop = messagesElement.value.scrollHeight
  },
)

onMounted(() => {
  void loadConversations(true)
  void conversationsApi.runtime().then((value) => {
    if (!disposed) runtimeInfo.value = value
  }).catch(() => {
    if (!disposed) runtimeError.value = '无法确认模型和数仓查询模式，请刷新后重试。'
  })
})
onBeforeUnmount(() => {
  disposed = true
  ++detailRequestSequence
  ++listRequestSequence
  clearPoll()
  for (const [entry, { id, controller }] of activeStreams) {
    if (outbox.get(id) === entry) {
      entry.state = 'retryable'
      entry.error = '页面已离开，连接已取消；回到此会话后可刷新历史或重试原请求。'
    }
    controller.abort()
  }
  activeStreams.clear()
})
</script>

<template>
  <div class="na-chat">
    <aside class="na-card na-chat__sidebar" aria-label="会话历史">
      <div class="na-card__head">
        <h2>会话历史</h2>
        <button class="na-btn" type="button" :disabled="creating || demoPreparing || conversationMutationPending" @click="createConversation">
          {{ creating ? '创建中…' : '新对话' }}
        </button>
      </div>
      <button
        class="na-btn na-chat__refresh"
        type="button"
        :disabled="conversationsLoading"
        @click="loadConversations(false)"
      >
        {{ conversationsLoading ? '刷新中…' : '刷新会话列表' }}
      </button>
      <p v-if="conversationsError" class="na-message na-message--error" role="alert">
        {{ conversationsError }}
      </p>
      <div v-if="lastDeletedConversation" class="na-chat__undo" role="status">
        <p class="na-muted">已隐藏“{{ lastDeletedConversation.title || '新对话' }}”，历史记录保留。</p>
        <button
          class="na-btn"
          type="button"
          :disabled="conversationMutationPending || creating || demoPreparing"
          @click="undoConversationDeletion"
        >
          {{ restoringConversation ? '恢复中…' : '撤销删除' }}
        </button>
      </div>
      <p v-if="!conversations.length && !conversationsLoading" class="na-muted">
        还没有会话，发送消息即可开始。
      </p>
      <ul class="na-chat__history">
        <li v-for="item in conversations" :key="item.id" class="na-chat__history-row">
          <button
            class="na-chat__conversation"
            type="button"
            :class="{ 'is-active': item.id === selectedConversationId }"
            :aria-current="item.id === selectedConversationId ? 'page' : undefined"
            :disabled="creating || deletingConversationId === item.id"
            @click="selectConversation(item.id)"
          >
            <span>{{ item.title || '新对话' }}</span>
            <small>{{ formatDateTime(item.updated_at) }}</small>
          </button>
          <button
            class="na-btn na-chat__delete"
            type="button"
            :aria-label="`删除会话：${item.title || '新对话'}`"
            :disabled="deletionDisabled(item.id)"
            title="从列表隐藏，本页可撤销；历史记录和热点运行保留"
            @click="deleteConversation(item)"
          >
            {{ deletingConversationId === item.id ? '删除中…' : '删除' }}
          </button>
        </li>
      </ul>
    </aside>

    <section class="na-card na-chat__main" aria-label="Agent 对话">
      <div class="na-card__head">
        <h2>{{ conversation?.title || '会话 Agent' }}</h2>
        <button
          v-if="selectedConversationId"
          class="na-btn"
          type="button"
          :disabled="detailLoading"
          @click="loadDetail(selectedConversationId)"
        >
          {{ detailLoading ? '加载中…' : '刷新历史' }}
        </button>
      </div>
      <p class="na-muted na-chat__context">
        查询问题 → 数仓 SQL → 热点模型分析 → 会话答复。仅查询与分析，不发布、不审批；上下文达到长度预算后压缩较早对话，保留摘要与最近完整消息。
      </p>
      <p class="na-muted na-chat__context">
        安全事件流实时展示处理阶段和工具状态；答复经完整校验并保存后分段传输。
      </p>
      <p v-if="runtimeInfo?.model_provider === 'local'" class="na-notice">
        当前使用本地确定性替身验证交互链路，不代表企业模型质量。
      </p>
      <p v-else-if="runtimeInfo?.model_provider === 'openai_compatible'" class="na-notice">
        兼容模型接口模式 · {{ runtimeInfo.model_route }}。数仓仍为合成样本，接口配置不代表模型质量已验收。
      </p>
      <p v-if="runtimeInfo?.query_enabled && runtimeInfo.query_scope" class="na-muted">
        未指定日期时使用固定样本窗口，默认场景 {{ runtimeInfo.query_scope.scenario_id }}：
        {{ formatDateTime(runtimeInfo.query_scope.window_start) }} 至
        {{ formatDateTime(runtimeInfo.query_scope.window_end) }}。
        请求“今日热点”会核对数据范围；样本不覆盖今天时会明确提示。当前样本不提供实时今日新闻。
        已读取运行的分析范围以各结果来源为准。
      </p>
      <p v-else-if="runtimeInfo" class="na-muted">
        当前未授权新数仓查询，仅可读取已完成热点报告和批准的新闻知识。
      </p>
      <p v-if="runtimeError" class="na-message na-message--error">{{ runtimeError }}</p>
      <p v-if="detailError" class="na-message na-message--error" role="alert">{{ detailError }}</p>
      <p v-if="detailLoading && !turns.length" class="na-muted" role="status">正在恢复会话历史…</p>

      <div
        ref="messagesElement"
        class="na-chat__messages"
        tabindex="0"
        role="log"
        aria-label="会话消息，可滚动阅读"
        aria-live="polite"
        :aria-busy="pending?.state === 'sending' || serverProcessing"
      >
        <div v-if="!turns.length && !pendingVisible && !detailLoading" class="na-chat__welcome">
          <h3>从一个问题开始</h3>
          <p class="na-muted">输入指标查询问题，查看 SQL 和分析结果，再追问其中的新闻。</p>
        </div>
        <article v-for="turn in visibleTurns" :key="turn.id" class="na-chat__turn">
          <div class="na-chat__message na-chat__message--user">
            <div class="na-chat__message-head">
              <span>你</span><small>{{ formatDateTime(turn.created_at) }}</small>
            </div>
            <p class="na-chat__text">{{ turn.user_content }}</p>
          </div>
          <div class="na-chat__message na-chat__message--assistant">
            <div class="na-chat__message-head">
              <span>NewsAgent</span><span class="na-badge">{{ statusLabel(turn.status) }}</span>
            </div>
            <p v-if="turn.assistant_content" class="na-chat__text">{{ turn.assistant_content }}</p>
            <p v-else-if="turn.status === 'processing'" class="na-muted" role="status">
              Agent 正在处理，可调用只读工具。完成后展示实际结果。
            </p>
            <p v-else-if="turn.status === 'failed'" class="na-message na-message--error">
              本轮处理失败，可在下方明确发送一条新消息。
            </p>
            <p v-if="turn.error_code" class="na-muted">错误码：{{ turn.error_code }}</p>
            <template v-if="turn.status === 'completed'">
              <AnalysisResultCard v-for="(tool, index) in turn.tools" :key="`analysis-${turn.id}-${index}`" :trace="tool" />
            </template>
            <details v-if="turn.tools.length" class="na-chat__tools">
              <summary>查看工具调用记录（{{ turn.tools.length }}）</summary>
              <details
                v-for="(tool, index) in turn.tools"
                :key="`${turn.id}-${index}`"
                class="na-chat__tool"
              >
                <summary>
                  {{ tool.name }} · {{ toolStatusLabel(tool.status) }} · 尝试 {{ tool.attempts }} 次
                </summary>
                <p v-if="tool.error_code" class="na-muted">错误码：{{ tool.error_code }}</p>
                <h3>参数</h3>
                <pre class="na-chat__tool-json">{{ JSON.stringify(tool.arguments, null, 2) }}</pre>
                <h3>结果</h3>
                <pre class="na-chat__tool-json">{{ JSON.stringify(tool.result, null, 2) }}</pre>
              </details>
            </details>
            <details v-if="turn.model_request_ids.length" class="na-chat__audit">
              <summary>模型调用编号</summary>
              <p v-for="id in turn.model_request_ids" :key="id" class="na-mono">{{ id }}</p>
            </details>
          </div>
        </article>
        <article v-if="pendingVisible && pending" class="na-chat__turn">
          <div class="na-chat__message na-chat__message--user">
            <span>你</span>
            <p class="na-chat__text">{{ pending.request.content }}</p>
          </div>
          <div class="na-chat__message na-chat__message--assistant">
            <span>NewsAgent</span>
            <p class="na-muted" role="status">
              {{ pending.state === 'sending' ? pending.phase : '本次请求的结果尚未确认。' }}
            </p>
            <ul v-if="pending.tools.length" class="na-chat__tool-progress">
              <li v-for="tool in pending.tools" :key="tool.index">
                {{ tool.name }} ·
                {{ tool.status === 'running' ? '调用中' : toolStatusLabel(tool.status) }}
                <span v-if="tool.attempts !== null"> · 尝试 {{ tool.attempts }} 次</span>
                <span v-if="tool.error_code"> · {{ tool.error_code }}</span>
              </li>
            </ul>
            <p v-if="pending.partialAnswer" class="na-chat__text">{{ pending.partialAnswer }}</p>
            <small v-if="pending.partialAnswer" class="na-muted">
              {{
                pending.state === 'sending'
                  ? '正在接收答复，最终以已保存结果为准'
                  : '连接已中断，以上为暂存内容，请刷新确认最终结果'
              }}
            </small>
          </div>
        </article>
      </div>

      <div v-if="pending?.state === 'retryable'" class="na-chat__retry">
        <p class="na-message na-message--error" role="alert">{{ pending.error }}</p>
        <button class="na-btn" type="button" :disabled="detailLoading" @click="retryMessage">
          重试原请求
        </button>
      </div>
      <details class="na-chat__examples">
        <summary>其他示例问题</summary>
        <p class="na-muted">点击示例只填入草稿，确认问题后再点击发送。</p>
        <div class="na-row" aria-label="示例问题">
          <button
            v-for="example in examples"
            :key="example"
            class="na-btn"
            type="button"
            :disabled="composerLocked"
            @click="useExample(example)"
          >
            {{ example }}
          </button>
        </div>
      </details>
      <AnalysisDemoPanel
        :runtime="runtimeInfo"
        :runtime-error="runtimeError"
        :local-demo-mode="localAnalysisDemoMode"
        :disabled="composerLocked || conversationMutationPending"
        :context-key="selectedConversationId ?? ''"
        @example="useExample"
        @busy="demoPreparing = $event"
        @prepared="onDemoPrepared"
      />
      <form class="na-chat__composer" @submit.prevent="sendMessage">
        <label for="chat-message" class="na-muted">消息</label>
        <textarea
          id="chat-message"
          v-model="draft"
          class="na-textarea"
          rows="3"
          maxlength="4000"
          :readonly="composerLocked"
          placeholder="输入问题，Ctrl / ⌘ + Enter 发送"
          @keydown="onComposerKeydown"
        />
        <div class="na-chat__composer-actions">
          <small class="na-muted">
            {{ draftLength }} / 4000 · 刷新页面可从服务端恢复已保存历史
          </small>
          <button
            class="na-btn na-btn--primary"
            type="submit"
            :disabled="composerLocked || draftLength === 0 || draftLength > 4000"
          >
            {{ pending?.state === 'sending' || serverProcessing ? '处理中…' : '发送' }}
          </button>
        </div>
        <p v-if="composerError" class="na-message na-message--error" role="alert">
          {{ composerError }}
        </p>
      </form>
    </section>
  </div>
</template>

<style scoped>
.na-chat {
  display: grid;
  grid-template-columns: minmax(220px, 270px) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}
.na-chat__sidebar {
  position: sticky;
  top: 16px;
}
.na-chat__refresh {
  width: 100%;
}
.na-chat__history {
  list-style: none;
  padding: 0;
  margin: 12px 0 0;
  max-height: 65vh;
  overflow-y: auto;
}
.na-chat__history-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}
.na-chat__delete {
  flex: 0 0 auto;
  padding: 6px 8px;
}
.na-chat__undo {
  margin-top: 12px;
}
.na-chat__undo p {
  overflow-wrap: anywhere;
}
.na-chat__conversation {
  display: flex;
  flex-direction: column;
  gap: 3px;
  flex: 1 1 auto;
  min-width: 0;
  text-align: left;
  padding: 10px;
  border: 1px solid transparent;
  border-radius: var(--na-radius-sm);
  background: transparent;
  color: var(--na-text);
  font: inherit;
  cursor: pointer;
}
.na-chat__conversation span {
  overflow-wrap: anywhere;
}
.na-chat__conversation small {
  color: var(--na-text-muted);
}
.na-chat__conversation:hover {
  background: var(--na-bg);
}
.na-chat__conversation.is-active {
  background: var(--na-accent-weak);
  border-color: var(--na-accent);
}
.na-chat__main {
  min-width: 0;
}
.na-chat__context {
  margin: 0 0 16px;
}
.na-chat__messages {
  display: flex;
  flex-direction: column;
  gap: 20px;
  height: clamp(300px, 48vh, 560px);
  min-height: 300px;
  overflow-y: auto;
  padding: 2px 4px 16px;
}
.na-chat__welcome {
  margin: auto;
  text-align: center;
  padding: 32px 0;
}
.na-chat__messages:focus-visible {
  outline: 2px solid var(--na-accent);
  outline-offset: 2px;
}
.na-chat__turn {
  display: flex;
  flex-direction: column;
  flex: 0 0 auto;
  gap: 10px;
}
.na-chat__message {
  padding: 12px 16px;
  border-radius: var(--na-radius);
  border: 1px solid var(--na-border);
  overflow-wrap: anywhere;
}
.na-chat__message--user {
  align-self: flex-end;
  width: min(90%, 900px);
  background: var(--na-accent-weak);
}
.na-chat__message--assistant {
  align-self: flex-start;
  width: min(100%, 1000px);
  background: var(--na-bg);
}
.na-chat__message-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  color: var(--na-text-muted);
}
.na-chat__text {
  white-space: pre-wrap;
  margin: 8px 0 0;
}
.na-chat__tools,
.na-chat__audit {
  margin-top: 12px;
}
summary {
  cursor: pointer;
  color: var(--na-text-muted);
}
.na-chat__tool {
  margin-top: 10px;
  padding: 8px 12px;
  border: 1px solid var(--na-border);
  border-radius: var(--na-radius-sm);
}
.na-chat__tool h3 {
  margin-top: 12px;
}
.na-chat__tool-json {
  overflow: auto;
  max-height: 260px;
  white-space: pre-wrap;
  margin: 6px 0;
}
.na-chat__examples {
  margin: 16px 0 10px;
}
.na-chat__examples .na-row {
  margin-top: 8px;
}
.na-chat__composer {
  border-top: 1px solid var(--na-border);
  padding-top: 12px;
}
.na-chat__composer-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 10px;
}
.na-chat__retry {
  margin-top: 12px;
}
@media (max-width: 800px) {
  .na-chat {
    grid-template-columns: minmax(0, 1fr);
  }
  .na-chat__sidebar {
    position: static;
  }
  .na-chat__history {
    max-height: 180px;
  }
  .na-chat__composer-actions {
    align-items: flex-start;
  }
}
</style>
