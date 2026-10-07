import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { jobsApi, openJobEventStream } from '@/api'
import type {
  DecisionAcceptedResponse,
  HumanDecisionRequest,
  JobEventStreamHandle,
  ListWritingJobsQuery,
  ProgressEvent,
  PublishJobRequest,
  PublishJobResponse,
  RecoveryPlanResponse,
  ResearchMetricsResponse,
  ResearchPackage,
  ResumeJobRequest,
  ResumeJobResponse,
  WorkflowProgressResponse,
  WritingJob,
} from '@/api'
import { describeError } from '@/utils/errors'

/** 事件流保留上限，避免长任务把内存吃满。 */
const MAX_BUFFERED_EVENTS = 500

export const DEFAULT_PAGE_SIZE = 20

export type StreamState = 'idle' | 'connecting' | 'open' | 'error'

export type JobInspectorTab =
  | 'overview'
  | 'progress'
  | 'research'
  | 'package'
  | 'recovery'
  | 'events'

/**
 * 写作任务工作台状态。
 *
 * 覆盖 `app/api/jobs.py` 的全部 12 个端点：列表、创建、详情、进度、
 * 研究指标、资料包、恢复计划、恢复执行、人工决策、发布、两个导出。
 * 身份头由网关注入，前端不参与。
 */
export const useJobsStore = defineStore('jobs', () => {
  /* ---------------- 列表 ---------------- */
  const items = ref<WritingJob[]>([])
  const total = ref(0)
  const limit = ref(DEFAULT_PAGE_SIZE)
  const offset = ref(0)
  const loading = ref(false)
  const listError = ref<string | null>(null)

  /* ---------------- 选中任务 ---------------- */
  const selectedId = ref<string | null>(null)
  const detail = ref<WritingJob | null>(null)
  const detailLoading = ref(false)
  const detailError = ref<string | null>(null)

  const activeTab = ref<JobInspectorTab>('overview')

  /* ---------------- 详情子资源 ---------------- */
  const progress = ref<WorkflowProgressResponse | null>(null)
  const progressLoading = ref(false)
  const progressError = ref<string | null>(null)

  const metrics = ref<ResearchMetricsResponse | null>(null)
  const metricsLoading = ref(false)
  const metricsError = ref<string | null>(null)

  const researchPackage = ref<ResearchPackage | null>(null)
  const packageLoading = ref(false)
  const packageError = ref<string | null>(null)

  const recovery = ref<RecoveryPlanResponse | null>(null)
  const recoveryLoading = ref(false)
  const recoveryError = ref<string | null>(null)

  /* ---------------- 事件流 ---------------- */
  const events = ref<ProgressEvent[]>([])
  const streamState = ref<StreamState>('idle')
  const streamError = ref<string | null>(null)
  const streamAttempt = ref(0)
  let handle: JobEventStreamHandle | null = null

  const selected = computed<WritingJob | null>(() => {
    if (!selectedId.value) return null
    if (detail.value?.id === selectedId.value) return detail.value
    return items.value.find((item) => item.id === selectedId.value) ?? null
  })

  function describe(cause: unknown): string {
    return describeError(cause)
  }

  /** 用最新数据合并列表项，保持列表与详情一致。 */
  function mergeIntoList(job: WritingJob): void {
    const index = items.value.findIndex((item) => item.id === job.id)
    if (index === -1) return
    const next = [...items.value]
    next[index] = { ...next[index], ...job }
    items.value = next
  }

  /* ---------------- 列表与创建 ---------------- */

  async function fetchList(query: ListWritingJobsQuery = {}): Promise<void> {
    loading.value = true
    listError.value = null
    try {
      const page = await jobsApi.list(query)
      items.value = page.items
      total.value = page.total
      limit.value = page.limit || limit.value
      offset.value = page.offset
    } catch (cause) {
      listError.value = describe(cause)
    } finally {
      loading.value = false
    }
  }

  async function fetchDetail(jobId: string): Promise<WritingJob | null> {
    detailLoading.value = true
    detailError.value = null
    try {
      const job = await jobsApi.get(jobId)
      if (selectedId.value === jobId) {
        detail.value = job
        mergeIntoList(job)
      }
      return job
    } catch (cause) {
      detailError.value = describe(cause)
      return null
    } finally {
      detailLoading.value = false
    }
  }

  /* ---------------- 详情子资源（按需加载，加载过则复用） ---------------- */

  async function fetchProgress(jobId: string, force = false): Promise<void> {
    if (!force && progress.value?.job_id === jobId) return
    progressLoading.value = true
    progressError.value = null
    try {
      progress.value = await jobsApi.progress(jobId)
    } catch (cause) {
      progressError.value = describe(cause)
    } finally {
      progressLoading.value = false
    }
  }

  async function fetchMetrics(jobId: string, force = false): Promise<void> {
    if (!force && metrics.value?.job_id === jobId) return
    metricsLoading.value = true
    metricsError.value = null
    try {
      metrics.value = await jobsApi.researchMetrics(jobId)
    } catch (cause) {
      metricsError.value = describe(cause)
    } finally {
      metricsLoading.value = false
    }
  }

  async function fetchPackage(jobId: string, force = false): Promise<void> {
    if (!force && researchPackage.value) return
    packageLoading.value = true
    packageError.value = null
    try {
      researchPackage.value = await jobsApi.researchPackage(jobId)
    } catch (cause) {
      packageError.value = describe(cause)
    } finally {
      packageLoading.value = false
    }
  }

  async function fetchRecovery(jobId: string, force = false): Promise<void> {
    if (!force && recovery.value?.job_id === jobId) return
    recoveryLoading.value = true
    recoveryError.value = null
    try {
      recovery.value = await jobsApi.recovery(jobId)
    } catch (cause) {
      recoveryError.value = describe(cause)
    } finally {
      recoveryLoading.value = false
    }
  }

  /** 按 tab 懒加载：只有打开对应面板才发请求。 */
  async function ensureTabData(jobId: string, tab: JobInspectorTab, force = false): Promise<void> {
    switch (tab) {
      case 'progress':
        await fetchProgress(jobId, force)
        break
      case 'research':
        await Promise.all([fetchMetrics(jobId, force), fetchProgress(jobId, force)])
        break
      case 'package':
        await fetchPackage(jobId, force)
        break
      case 'recovery':
        await Promise.all([fetchRecovery(jobId, force), fetchDetail(jobId)])
        break
      default:
        break
    }
  }

  /* ---------------- 选中与事件流 ---------------- */

  function select(jobId: string | null): void {
    disconnectStream()
    selectedId.value = jobId
    detail.value = null
    detailError.value = null
    progress.value = null
    metrics.value = null
    researchPackage.value = null
    recovery.value = null
    events.value = []
    streamError.value = null
    streamAttempt.value = 0
    activeTab.value = 'overview'
  }

  /** 把事件流回写到详情与列表，保证不刷新也能看到进度推进。 */
  function applyEvent(event: ProgressEvent): void {
    events.value = [...events.value, event].slice(-MAX_BUFFERED_EVENTS)
    const patch: Partial<WritingJob> = {
      status: event.status,
      progress_percent: event.progress.percent,
      current_step: event.step,
    }
    const current = detail.value
    if (current && current.id === event.job_id) {
      detail.value = { ...current, ...patch, updated_at: event.occurred_at }
    }
    const index = items.value.findIndex((item) => item.id === event.job_id)
    if (index !== -1) {
      const next = [...items.value]
      next[index] = { ...next[index], ...patch, updated_at: event.occurred_at }
      items.value = next
    }
  }

  function connectStream(jobId: string, lastEventId?: string): void {
    disconnectStream()
    streamState.value = 'connecting'
    streamError.value = null
    handle = openJobEventStream({
      jobId,
      ...(lastEventId ? { lastEventId } : {}),
      onOpen: () => {
        streamState.value = 'open'
        streamError.value = null
      },
      onEvent: (event) => {
        streamAttempt.value = 0
        applyEvent(event)
      },
      onError: (cause, attempt) => {
        streamState.value = 'error'
        streamAttempt.value = attempt
        streamError.value = `${cause instanceof Error ? cause.message : String(cause)}（第 ${attempt} 次重试）`
      },
    })
  }

  function disconnectStream(): void {
    handle?.close()
    handle = null
    streamState.value = 'idle'
    streamAttempt.value = 0
  }

  /* ---------------- 写操作 ---------------- */

  async function resume(jobId: string, body: ResumeJobRequest): Promise<ResumeJobResponse> {
    const result = await jobsApi.resume(jobId, body)
    await fetchDetail(jobId)
    await fetchRecovery(jobId, true)
    return result
  }

  async function decide(
    jobId: string,
    body: HumanDecisionRequest,
  ): Promise<DecisionAcceptedResponse> {
    const result = await jobsApi.decide(jobId, body)
    await fetchDetail(jobId)
    return result
  }

  async function publish(jobId: string, body: PublishJobRequest = {}): Promise<PublishJobResponse> {
    const result = await jobsApi.publish(jobId, body)
    await fetchDetail(jobId)
    return result
  }

  /** 打开选中任务并开始接收事件流。 */
  function openJob(jobId: string): void {
    select(jobId)
    void fetchDetail(jobId)
    connectStream(jobId)
  }

  return {
    items,
    total,
    limit,
    offset,
    loading,
    listError,
    selectedId,
    selected,
    detail,
    detailLoading,
    detailError,
    activeTab,
    progress,
    progressLoading,
    progressError,
    metrics,
    metricsLoading,
    metricsError,
    researchPackage,
    packageLoading,
    packageError,
    recovery,
    recoveryLoading,
    recoveryError,
    events,
    streamState,
    streamError,
    streamAttempt,
    fetchList,
    fetchDetail,
    fetchProgress,
    fetchMetrics,
    fetchPackage,
    fetchRecovery,
    ensureTabData,
    select,
    openJob,
    connectStream,
    disconnectStream,
    resume,
    decide,
    publish,
  }
})
