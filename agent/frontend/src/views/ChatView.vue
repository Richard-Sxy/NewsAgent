<script lang="ts">
import { reactive as reactiveOutbox } from 'vue'
import type { ConversationToolTrace, SendConversationMessage } from '@/api/conversations'

interface PendingMessage {
  request: SendConversationMessage
  state: 'sending' | 'retryable'
  error: string | null
  phase: string
  partialAnswer: string
  turnId: string | null
  replayed: boolean
  savedTools: ConversationToolTrace[]
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
import { formatTurnDuration, turnDurationSeconds } from '@/utils/turnDuration'
import { preferCapabilityCards } from '@/utils/conversationPresentation'
import { useConfirmStore } from '@/stores/confirm'
import { useToastStore } from '@/stores/toast'
import AnalysisDemoPanel from '@/components/conversation/AnalysisDemoPanel.vue'
import AnalysisResultCard from '@/components/conversation/AnalysisResultCard.vue'
import RelatedArticlesPanel from '@/components/conversation/RelatedArticlesPanel.vue'
import CapabilityReceiptsPanel from '@/components/conversation/CapabilityReceiptsPanel.vue'

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
const historyExpanded = ref(false)
// 只保留当前页面内的草稿和未确认请求，不把消息写进浏览器持久存储。
const drafts = reactive<Record<string, string>>(Object.create(null))
const outbox = conversationOutbox
const activeStreams = new Map<PendingMessage, { id: string; controller: AbortController }>()
const examples = ['看看热点新闻', '今日热点', '查询点击率最高的前5条视频新闻并分析原因', '解释第一条新闻', '检索人工智能相关新闻']
const runtimeInfo = ref<ConversationRuntime | null>(null)
const runtimeError = ref<string | null>(null)
const writingEnabled = computed(() => runtimeInfo.value?.available_tools?.includes('start_writing_intent') ?? false)
const approvedCapabilities = computed(() => {
  const tools = new Set(runtimeInfo.value?.available_tools ?? [])
  const capabilities: string[] = []
  if (tools.has('list_writing_jobs') || tools.has('writing_job_status')) capabilities.push('查询写作任务与进度')
  if (tools.has('start_writing_intent')) capabilities.push('发起写作')
  if (tools.has('save_short_term_memory')) capabilities.push('保存短期记忆')
  if (tools.has('propose_long_term_memory')) capabilities.push('提交长期记忆候选')
  if (tools.has('memory_status')) capabilities.push('查询记忆状态')
  if (tools.has('save_feedback_case')) capabilities.push('记录分析驳回反馈')
  if (tools.has('propose_bundle_candidate')) capabilities.push('提交配置候选')
  if (tools.has('start_data_loop')) capabilities.push('发起评测')
  if (tools.has('evaluation_status')) capabilities.push('查询评测状态')
  return capabilities
})
const confirmationCapabilities = computed(() => {
  const tools = new Set(runtimeInfo.value?.available_tools ?? [])
  return [['start_writing_intent', '写作'], ['save_feedback_case', '分析驳回反馈'],
    ['propose_bundle_candidate', '配置候选'], ['start_data_loop', '评测']]
    .filter(([tool]) => tools.has(tool)).map(([, label]) => label)
})
const newsSummary = computed(() => runtimeInfo.value?.query_enabled
  ? '输入热点问题，查询数仓、分析新闻并追问结果。'
  : '可读取已保存的热点报告、检索新闻知识并追问分析。')
const quickExamples = computed(() => {
  const tools = new Set(runtimeInfo.value?.available_tools ?? [])
  const values: Array<{ label: string; content: string }> = []
  if (tools.has('query_hot_news')) values.push({ label: '看看热点', content: '查看当前样本的热点新闻' })
  else if (tools.has('list_hot_news')) values.push({ label: '读取热点报告', content: '看看热点新闻' })
  if (tools.has('start_writing_intent')) values.push({ label: '写一篇文章', content: '写一篇人工智能发展趋势，800字，客观中立' })
  if (tools.has('save_short_term_memory')) values.push({ label: '记住我的偏好', content: '记住：我更关注科技新闻' })
  if (tools.has('list_writing_jobs')) values.push({ label: '查看写作任务', content: '我有哪些写作任务' })
  return values
})
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
  entry.savedTools = []
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
    if (event.data.trace) {
      entry.savedTools.push(event.data.trace)
      if (selectedConversationId.value === id) {
        const processing = turns.value.find((turn) => turn.id === entry.turnId && turn.status === 'processing')
        if (processing && processing.tools.length <= event.data.index) processing.tools.push(event.data.trace)
      }
    }
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
    savedTools: [],
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

function redescribeWriting(content: string): void {
  if (!disposed && selectedConversationId.value && !conversationMutationPending.value) draft.value = content
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

function turnDuration(turn: ConversationTurn): string | null {
  return formatTurnDuration(turnDurationSeconds(turn))
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
  void loadConversations()
  void conversationsApi.runtime().then((value) => {
    if (!disposed) runtimeInfo.value = value
  }).catch(() => {
    if (!disposed) runtimeError.value = '无法确认当前可用功能，请刷新后重试。'
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
      <div class="na-card__head na-chat__sidebar-head">
        <h2>最近会话 <span v-if="conversations.length" class="na-chat__history-count">{{ conversations.length }}</span></h2>
        <button class="na-btn na-chat__history-toggle" type="button" :aria-expanded="historyExpanded" @click="historyExpanded = !historyExpanded">
          {{ historyExpanded ? '收起' : '展开' }}
        </button>
      </div>
      <button class="na-btn na-chat__new" type="button" :disabled="creating || demoPreparing || conversationMutationPending" @click="createConversation">
        <svg viewBox="0 0 20 20" width="16" height="16" fill="none" aria-hidden="true"><path d="M10 4v12M4 10h12" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" /></svg>
        {{ creating ? '创建中…' : '新对话' }}
      </button>
      <div class="na-chat__history-content" :class="{ 'is-expanded': historyExpanded }">
        <div class="na-chat__history-toolbar">
          <span class="na-muted">历史记录</span>
          <button
            class="na-btn na-chat__refresh"
            type="button"
            :disabled="conversationsLoading"
            @click="loadConversations(false)"
          >
            {{ conversationsLoading ? '刷新中…' : '刷新' }}
          </button>
        </div>
        <p v-if="conversationsError" class="na-message na-message--error" role="alert">
          {{ conversationsError }}
        </p>
        <div v-if="lastDeletedConversation" class="na-chat__undo" role="status">
          <p class="na-muted">会话已删除，历史记录保留，可撤销本次删除。</p>
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
              :title="item.title || '新对话'"
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
              <span v-if="deletingConversationId === item.id">…</span>
              <svg v-else viewBox="0 0 20 20" width="15" height="15" fill="none" aria-hidden="true"><path d="M4 5.5h12M8 5.5V3h4v2.5M6 5.5l.7 11h6.6l.7-11M8.5 8.5v5M11.5 8.5v5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" /></svg>
            </button>
          </li>
        </ul>
      </div>
    </aside>

    <section class="na-card na-chat__main" aria-label="Agent 对话">
      <div class="na-card__head">
        <h2>{{ conversation?.title || '新闻对话' }}</h2>
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
      <div class="na-chat__intro">
        <p class="na-muted">让新闻发现、分析与写作更顺手。</p>
        <span v-if="runtimeInfo?.query_scope?.dataset_profile === 'corpus-v6' && runtimeInfo.query_scope.news_content_source === 'tencent-news-cache' && runtimeInfo.query_scope.metric_data_synthetic === true" class="na-chat__mode">真实新闻 · 模拟指标 · 本地模型</span>
        <span v-else-if="runtimeInfo?.model_provider === 'local'" class="na-chat__mode">本地模拟 · 非实时样本</span>
        <span v-else-if="runtimeInfo?.query_scope" class="na-chat__mode">样本数据 · 非实时新闻</span>
      </div>
      <p v-if="runtimeError" class="na-message na-message--error">{{ runtimeError }}</p>
      <p v-if="detailError" class="na-message na-message--error" role="alert">{{ detailError }}</p>
      <p v-if="detailLoading && !turns.length" class="na-muted" role="status">正在恢复会话历史…</p>

      <div
        ref="messagesElement"
        class="na-chat__messages"
        :class="{ 'na-chat__messages--empty': !turns.length && !pendingVisible && !detailLoading }"
        tabindex="0"
        role="log"
        aria-label="会话消息，可滚动阅读"
        aria-live="polite"
        :aria-busy="pending?.state === 'sending' || serverProcessing"
      >
        <div v-if="!turns.length && !pendingVisible && !detailLoading" class="na-chat__welcome">
          <div class="na-chat__welcome-icon" aria-hidden="true">
            <svg viewBox="0 0 32 32" width="28" height="28" fill="none"><path d="M8 7h16a3 3 0 0 1 3 3v10a3 3 0 0 1-3 3H13l-6 4v-4a3 3 0 0 1-3-3V10a3 3 0 0 1 4-3Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" /><path d="M10 13h12M10 18h8" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" /></svg>
          </div>
          <span class="na-chat__welcome-label">NEWSAGENT</span>
          <h3>今天，想了解什么？</h3>
          <p class="na-muted">{{ newsSummary }}</p>
          <div class="na-chat__quick-examples" aria-label="开始对话的示例">
            <button v-for="example in quickExamples" :key="example.label" class="na-btn na-chat__quick-example" type="button" :disabled="composerLocked" @click="useExample(example.content)">
              <span>{{ example.label }}</span>
              <svg viewBox="0 0 20 20" width="15" height="15" fill="none" aria-hidden="true"><path d="M5 10h10M11 6l4 4-4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" /></svg>
            </button>
          </div>
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
              <span>NewsAgent</span><span v-if="turn.status !== 'completed'" class="na-badge">{{ statusLabel(turn.status) }}</span>
            </div>
            <p v-if="turn.assistant_content && !preferCapabilityCards(turn)" class="na-chat__text">{{ turn.assistant_content }}</p>
            <p v-else-if="turn.status === 'processing'" class="na-muted" role="status">
              Agent 正在处理，已保存的工具结果会显示在下方。
            </p>
            <p v-else-if="turn.status === 'failed'" class="na-message na-message--error">
              本轮处理失败，可在下方明确发送一条新消息。
            </p>
            <template v-if="turn.status === 'completed'">
              <AnalysisResultCard v-for="(tool, index) in turn.tools" :key="`analysis-${turn.id}-${index}`" :trace="tool" />
              <RelatedArticlesPanel :tools="turn.tools" />
            </template>
            <CapabilityReceiptsPanel
              :tools="turn.tools"
              :conversation-id="selectedConversationId ?? ''"
              :turn-id="turn.id"
              :active="turn.status === 'processing'"
              :writing-enabled="writingEnabled"
              :available-tools="runtimeInfo?.available_tools"
              :inputs="turn.inputs"
              :original-content="turn.user_content"
              @redescribe="redescribeWriting"
            />
            <details v-if="turn.assistant_content && preferCapabilityCards(turn)" class="na-disclosure">
              <summary>完整答复</summary>
              <p class="na-chat__text">{{ turn.assistant_content }}</p>
            </details>
            <details v-if="turn.tools.length || turn.model_request_ids.length || turn.error_code || turnDuration(turn) !== null" class="na-chat__tools na-disclosure">
              <summary>处理详情</summary>
              <p v-if="turn.error_code" class="na-muted">错误码：{{ turn.error_code }}</p>
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
              <details v-if="turn.model_request_ids.length" class="na-chat__audit">
                <summary>模型调用编号</summary>
                <p v-for="id in turn.model_request_ids" :key="id" class="na-mono">{{ id }}</p>
              </details>
              <p v-if="turnDuration(turn) !== null" class="na-chat__duration">
                全链路耗时 {{ turnDuration(turn) }}s
              </p>
            </details>
          </div>
        </article>
        <article v-if="pendingVisible && pending" class="na-chat__turn">
          <div class="na-chat__message na-chat__message--user">
            <div class="na-chat__message-head"><span>你</span><small>刚刚</small></div>
            <p class="na-chat__text">{{ pending.request.content }}</p>
          </div>
          <div class="na-chat__message na-chat__message--assistant">
            <div class="na-chat__message-head"><span>NewsAgent</span><span v-if="pending.state === 'sending'" class="na-chat__processing">处理中</span></div>
            <p class="na-muted" role="status">
              {{ pending.state === 'sending' ? pending.phase : '本次请求的结果尚未确认。' }}
            </p>
            <details v-if="pending.tools.length" class="na-disclosure">
              <summary>处理详情</summary>
              <ul class="na-chat__tool-progress">
                <li v-for="tool in pending.tools" :key="tool.index">
                  {{ tool.name }} ·
                  {{ tool.status === 'running' ? '调用中' : toolStatusLabel(tool.status) }}
                  <span v-if="tool.attempts !== null"> · 尝试 {{ tool.attempts }} 次</span>
                  <span v-if="tool.error_code"> · {{ tool.error_code }}</span>
                </li>
              </ul>
            </details>
            <p v-if="pending.partialAnswer" class="na-chat__text">{{ pending.partialAnswer }}</p>
            <CapabilityReceiptsPanel
              :tools="pending.savedTools"
              :conversation-id="selectedConversationId ?? ''"
              :turn-id="pending.turnId ?? ''"
              :active="pending.state === 'sending'"
              :writing-enabled="writingEnabled"
              :available-tools="runtimeInfo?.available_tools"
              :inputs="turns.find((turn) => turn.id === pending?.turnId)?.inputs"
              :original-content="pending.request.content"
              @redescribe="redescribeWriting"
            />
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
      <details class="na-chat__examples na-disclosure">
        <summary>更多示例与使用说明</summary>
        <p class="na-muted">选择示例后，再点击发送。</p>
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
        <details class="na-disclosure na-chat__help">
          <summary>可用功能与数据范围</summary>
          <p class="na-muted">{{ newsSummary }}<template v-if="approvedCapabilities.length">还可{{ approvedCapabilities.join('、') }}。</template></p>
          <p v-if="confirmationCapabilities.length" class="na-muted">{{ confirmationCapabilities.join('、') }}需要先确认；任务审批在对应工作台完成。</p>
          <p v-if="runtimeInfo?.available_tools?.includes('propose_long_term_memory')" class="na-muted">长期记忆候选等待人工晋升后才生效。</p>
          <p v-if="runtimeInfo?.query_enabled && runtimeInfo.query_scope" class="na-muted">
            可查询 {{ formatDateTime(runtimeInfo.query_scope.coverage_window_start ?? runtimeInfo.query_scope.window_start) }} 至
            {{ formatDateTime(runtimeInfo.query_scope.coverage_window_end ?? runtimeInfo.query_scope.window_end) }}（结束不含），每次一个整点小时。
            未指定日期时使用样本窗口；“今日热点”在样本不覆盖今天时会明确提示。
          </p>
          <p v-if="runtimeInfo?.model_provider === 'local'" class="na-muted">本地模型用于测试交互链路，不代表企业模型质量。</p>
          <p v-if="runtimeInfo?.query_scope?.dataset_profile === 'corpus-v6'" class="na-muted">新闻标题与正文来自原文缓存；指标窗口为模拟日期，栏目为演示分类。</p>
        </details>
      </details>
      <form class="na-chat__composer" @submit.prevent="sendMessage">
        <label for="chat-message" class="na-chat__composer-label">你的问题</label>
        <textarea
          id="chat-message"
          v-model="draft"
          class="na-textarea"
          rows="2"
          maxlength="4000"
          :readonly="composerLocked"
          placeholder="问一个新闻问题，或描述你想完成的任务…"
          @keydown="onComposerKeydown"
        />
        <div class="na-chat__composer-actions">
          <small class="na-muted na-chat__composer-hint">
            <span class="na-chat__shortcut">Ctrl / ⌘ + Enter 发送</span><span v-if="draftLength > 3500"> · {{ draftLength }} / 4000</span>
          </small>
          <button
            class="na-btn na-btn--primary"
            type="submit"
            :disabled="composerLocked || draftLength === 0 || draftLength > 4000"
          >
            {{ pending?.state === 'sending' || serverProcessing ? '处理中…' : '发送' }}
            <svg viewBox="0 0 20 20" width="16" height="16" fill="none" aria-hidden="true"><path d="M10 15V5M6 9l4-4 4 4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" /></svg>
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
  grid-template-columns: 224px minmax(0, 1fr);
  gap: 20px;
  align-items: start;
}
.na-chat__sidebar {
  position: sticky;
  top: 88px;
  padding: 16px 12px;
  border-radius: 16px;
  background: #f2f4f1;
}
.na-chat__sidebar-head {
  margin: 0 4px 16px;
}
.na-chat__sidebar-head h2 {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
}
.na-chat__history-count {
  padding: 1px 6px;
  border-radius: 6px;
  background: #e6ebe4;
  color: var(--na-text-muted);
  font-size: 11px;
  font-weight: 500;
}
.na-chat__history-toggle { display: none; }
.na-chat__new {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  width: 100%;
  padding: 10px 12px;
  border-color: #cdded5;
  background: var(--na-surface);
  color: var(--na-accent);
  font-size: 13px;
  font-weight: 600;
  border-radius: 10px;
}
.na-chat__history-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 18px 4px 6px;
  font-size: 11px;
}
.na-chat__refresh {
  border-color: transparent;
  background: transparent;
  padding: 2px 4px;
  font-size: 11px;
  color: var(--na-text-muted);
}
.na-chat__history {
  list-style: none;
  padding: 0;
  margin: 0;
  max-height: 65vh;
  overflow-y: auto;
  scrollbar-width: thin;
}
.na-chat__history-row {
  display: flex;
  align-items: center;
  gap: 2px;
  margin-bottom: 4px;
  border-radius: 10px;
}
.na-chat__history-row:has(.is-active) {
  background: var(--na-surface);
}
.na-chat__delete {
  display: grid;
  place-items: center;
  flex: 0 0 27px;
  width: 27px;
  height: 30px;
  border-color: transparent;
  background: transparent;
  padding: 0;
  color: var(--na-text-muted);
  opacity: 0.5;
}
.na-chat__history-row:hover .na-chat__delete,
.na-chat__delete:focus-visible { opacity: 1; }
.na-chat__undo {
  margin-top: 12px;
  font-size: 12px;
}
.na-chat__undo p { overflow-wrap: anywhere; }
.na-chat__conversation {
  display: flex;
  flex-direction: column;
  gap: 5px;
  flex: 1 1 auto;
  min-width: 0;
  text-align: left;
  padding: 11px 10px;
  border: 1px solid transparent;
  border-radius: 10px;
  background: transparent;
  color: var(--na-text);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}
.na-chat__conversation span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.na-chat__conversation small {
  font-size: 10px;
  color: var(--na-text-muted);
}
.na-chat__conversation:hover { background: #e9eee7; }
.na-chat__conversation.is-active {
  background: var(--na-surface);
  color: var(--na-accent);
  font-weight: 600;
}
.na-chat__conversation:focus-visible {
  outline: 2px solid var(--na-accent);
  outline-offset: -2px;
}
.na-chat__main {
  min-width: 0;
  padding: 22px 24px 20px;
  border-radius: 16px;
}
.na-chat__main > .na-card__head { margin-bottom: 7px; }
.na-chat__main > .na-card__head h2 {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 17px;
  font-weight: 600;
  letter-spacing: -0.3px;
}
.na-chat__main > .na-card__head .na-btn {
  flex: 0 0 auto;
  padding: 5px 8px;
  border-color: transparent;
  font-size: 12px;
  color: var(--na-text-muted);
}
.na-chat__intro {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 12px;
  padding-bottom: 17px;
  border-bottom: 1px solid var(--na-border);
  font-size: 12px;
}
.na-chat__intro p { margin: 0; }
.na-chat__mode {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 7px;
  border-radius: 6px;
  background: #f2f4ef;
  font-size: 10px;
  color: var(--na-text-muted);
}
.na-chat__mode::before {
  content: '';
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: #b4bbab;
}
.na-chat__help { margin-top: 16px; }
.na-chat__quick-examples {
  display: flex;
  justify-content: center;
  flex-wrap: wrap;
  gap: 9px;
  margin-top: 26px;
}
.na-chat__quick-example {
  display: inline-flex;
  align-items: center;
  gap: 16px;
  padding: 12px 14px;
  border-color: var(--na-border);
  border-radius: 10px;
  font-size: 13px;
  text-align: left;
}
.na-chat__quick-example svg { color: #91a59a; }
.na-chat__messages {
  display: flex;
  flex-direction: column;
  gap: 26px;
  height: clamp(330px, 53vh, 680px);
  min-height: 330px;
  overflow-y: auto;
  padding: 24px 6px 16px;
  scrollbar-width: thin;
  scrollbar-color: #d9dfd7 transparent;
}
.na-chat__messages--empty { min-height: 330px; }
.na-chat__welcome {
  width: 100%;
  max-width: 640px;
  margin: auto;
  text-align: center;
  padding: 24px 8px;
}
.na-chat__welcome-icon {
  display: grid;
  place-items: center;
  width: 56px;
  height: 56px;
  margin: 0 auto 17px;
  border: 1px solid #d8e5dc;
  border-radius: 18px;
  background: #edf4ee;
  color: var(--na-accent);
}
.na-chat__welcome-label {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 2px;
  color: #85968a;
}
.na-chat__welcome h3 {
  margin: 12px 0;
  font-size: clamp(22px, 2.2vw, 30px);
  font-weight: 600;
  letter-spacing: -0.8px;
}
.na-chat__welcome > p {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
}
.na-chat__messages:focus-visible {
  outline: 2px solid var(--na-accent);
  outline-offset: 2px;
  border-radius: 10px;
}
.na-chat__turn {
  display: flex;
  flex-direction: column;
  flex: 0 0 auto;
  align-self: center;
  width: 100%;
  max-width: 900px;
  gap: 16px;
}
.na-chat__message {
  min-width: 0;
  overflow-wrap: anywhere;
}
.na-chat__message--user {
  align-self: flex-end;
  width: min(88%, 720px);
  padding: 13px 17px;
  border: 1px solid #e2e9df;
  border-radius: 14px 14px 4px 14px;
  background: #f0f4ee;
}
.na-chat__message--assistant {
  align-self: flex-start;
  width: 100%;
  padding: 2px 0 10px;
}
.na-chat__message-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-size: 12px;
  color: var(--na-text-muted);
}
.na-chat__message-head small { font-size: 10px; }
.na-chat__message--assistant .na-chat__message-head > span:first-child {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--na-text);
  font-weight: 600;
}
.na-chat__message--assistant .na-chat__message-head > span:first-child::before {
  content: 'N';
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  border-radius: 8px;
  background: var(--na-accent-weak);
  color: var(--na-accent);
  font-size: 11px;
}
.na-chat__processing { color: var(--na-accent); font-size: 11px; }
.na-chat__text {
  white-space: pre-wrap;
  margin: 10px 0 0;
  font-size: 14px;
  line-height: 1.85;
}
.na-chat__tools,
.na-chat__audit { margin-top: 14px; }
.na-chat__duration {
  margin: 10px 0 0;
  font-size: 11px;
  color: var(--na-text-muted);
  text-align: right;
}
summary {
  cursor: pointer;
  color: var(--na-text-muted);
}
.na-chat__tool {
  margin-top: 10px;
  padding: 8px 12px;
  border: 1px solid var(--na-border);
  border-radius: 10px;
}
.na-chat__tool h3 { margin-top: 12px; }
.na-chat__tool-json {
  overflow: auto;
  max-height: 260px;
  white-space: pre-wrap;
  margin: 6px 0;
  font-size: 11px;
}
.na-chat__examples { margin: 4px 0 12px; }
.na-chat__examples > summary { font-size: 11px; }
.na-chat__examples .na-row { margin-top: 8px; }
.na-chat__composer {
  position: relative;
  border: 1px solid #d3dfd5;
  border-radius: 14px;
  padding: 14px;
  background: var(--na-surface);
  box-shadow: 0 4px 16px #163d2410;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.na-chat__composer:focus-within {
  border-color: var(--na-accent);
  box-shadow: 0 0 0 3px var(--na-accent-weak);
}
.na-chat__composer-label {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
  border: 0;
}
.na-chat__composer .na-textarea {
  display: block;
  width: 100%;
  min-height: 64px;
  padding: 0;
  border: 0;
  border-radius: 0;
  line-height: 1.7;
  font-size: 14px;
}
.na-chat__composer .na-textarea:focus { outline: none; }
.na-chat__composer .na-textarea::placeholder { color: #9aa59a; }
.na-chat__composer-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 8px;
}
.na-chat__composer-hint { min-width: 0; font-size: 10px; }
.na-chat__composer-actions .na-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  gap: 7px;
  min-height: 36px;
  padding: 7px 13px;
  border-radius: 9px;
  font-size: 13px;
}
.na-chat__retry { margin-top: 12px; }
@media (max-width: 1180px) {
  .na-chat { grid-template-columns: 200px minmax(0, 1fr); gap: 16px; }
  .na-chat__main { padding: 20px; }
}
@media (max-width: 960px) {
  .na-chat { grid-template-columns: minmax(0, 1fr); }
  .na-chat__sidebar {
    position: static;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 12px;
    padding: 12px 16px;
  }
  .na-chat__sidebar-head { margin: 0; }
  .na-chat__sidebar-head h2 { flex: 0 0 auto; }
  .na-chat__new { width: auto; padding: 7px 10px; }
  .na-chat__history-toggle {
    display: inline-flex;
    margin-left: auto;
    border-color: transparent;
    background: transparent;
    font-size: 11px;
    padding: 3px;
    color: var(--na-text-muted);
  }
  .na-chat__history-content { display: none; grid-column: 1 / -1; }
  .na-chat__history-content.is-expanded { display: block; }
  .na-chat__history-toolbar { margin-top: 0; }
  .na-chat__history { max-height: 180px; }
}
@media (max-width: 600px) {
  .na-chat { gap: 12px; }
  .na-chat__sidebar { padding: 10px 12px; }
  .na-chat__sidebar-head { gap: 10px; }
  .na-chat__sidebar-head h2 { font-size: 12px; gap: 5px; }
  .na-chat__history-count { font-size: 10px; }
  .na-chat__new { font-size: 12px; }
  .na-chat__main { padding: 16px 14px; }
  .na-chat__main > .na-card__head h2 { font-size: 16px; }
  .na-chat__intro { padding-bottom: 12px; font-size: 11px; }
  .na-chat__messages {
    height: clamp(300px, 48vh, 540px);
    min-height: 300px;
    padding: 20px 0 12px;
  }
  .na-chat__messages--empty { height: auto; min-height: 260px; }
  .na-chat__welcome { padding: 12px 0; }
  .na-chat__welcome-icon { width: 48px; height: 48px; border-radius: 15px; margin-bottom: 12px; }
  .na-chat__welcome h3 { font-size: 23px; margin: 10px 0; }
  .na-chat__welcome > p { font-size: 12px; }
  .na-chat__quick-examples { gap: 7px; margin-top: 20px; }
  .na-chat__quick-example { font-size: 12px; padding: 9px 10px; gap: 9px; }
  .na-chat__message--user { padding: 11px 13px; }
  .na-chat__text { font-size: 13px; line-height: 1.8; }
  .na-chat__composer { padding: 12px; }
  .na-chat__composer .na-textarea { font-size: 13px; }
  .na-chat__shortcut { display: none; }
}
</style>
