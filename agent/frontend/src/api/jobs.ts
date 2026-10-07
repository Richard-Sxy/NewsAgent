import { request, requestBlob } from '@/api/http'
import type { QueryParams } from '@/api/http'
import type {
  CreateWritingJobRequest,
  DecisionAcceptedResponse,
  HumanDecisionRequest,
  ListWritingJobsQuery,
  PublishJobRequest,
  PublishJobResponse,
  RecoveryPlanResponse,
  ResearchMetricsResponse,
  ResearchPackage,
  ResumeJobRequest,
  ResumeJobResponse,
  WorkflowProgressResponse,
  WritingJob,
  WritingJobList,
} from '@/api/types'

const BASE = '/api/v1/jobs'

function toListQuery(query: ListWritingJobsQuery): QueryParams {
  return {
    // 后端使用 `status` 可重复查询参数，数组会在 buildUrl 中展开为多个同名键。
    status: query.status?.length ? query.status : undefined,
    waiting_human_only: query.waiting_human_only ? true : undefined,
    query: query.query,
    limit: query.limit,
    offset: query.offset,
  }
}

/**
 * 写作任务接口。
 * 所有身份与租户信息由网关注入，前端不传递 X-Tenant-ID / X-User-ID。
 */
export const jobsApi = {
  create(body: CreateWritingJobRequest) {
    return request<WritingJob>(BASE, { method: 'POST', body })
  },

  list(query: ListWritingJobsQuery = {}) {
    return request<WritingJobList>(BASE, { query: toListQuery(query) })
  },

  get(jobId: string) {
    return request<WritingJob>(`${BASE}/${jobId}`)
  },

  progress(jobId: string) {
    return request<WorkflowProgressResponse>(`${BASE}/${jobId}/progress`)
  },

  researchMetrics(jobId: string) {
    return request<ResearchMetricsResponse>(`${BASE}/${jobId}/research-metrics`)
  },

  researchPackage(jobId: string) {
    return request<ResearchPackage>(`${BASE}/${jobId}/research-package`)
  },

  recovery(jobId: string) {
    return request<RecoveryPlanResponse>(`${BASE}/${jobId}/recovery`)
  },

  resume(jobId: string, body: ResumeJobRequest) {
    return request<ResumeJobResponse>(`${BASE}/${jobId}/resume`, { method: 'POST', body })
  },

  decide(jobId: string, body: HumanDecisionRequest) {
    return request<DecisionAcceptedResponse>(`${BASE}/${jobId}/decisions`, {
      method: 'POST',
      body,
    })
  },

  publish(jobId: string, body: PublishJobRequest = {}) {
    return request<PublishJobResponse>(`${BASE}/${jobId}/publish`, { method: 'POST', body })
  },

  exportFinal(jobId: string) {
    return requestBlob(`${BASE}/${jobId}/export`)
  },

  exportResearchPackage(jobId: string) {
    return requestBlob(`${BASE}/${jobId}/research-package/export`)
  },
}
