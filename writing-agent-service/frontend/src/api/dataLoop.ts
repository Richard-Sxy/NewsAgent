import { request } from '@/api/http'
import type {
  ApproveFeedbackLabelRequest,
  BootstrapProductionBundleRequest,
  CandidateResponse,
  DataLoopDecisionResponse,
  DataLoopSnapshotResponse,
  FeedbackCaseListResponse,
  FeedbackLabelListResponse,
  FeedbackLabelResponse,
  FreezeDatasetRequest,
  FreezeDatasetResponse,
  OperatorDecisionResponse,
  ProductionBundleResponse,
  ProposeCandidateRequest,
  RecordOperatorDecisionRequest,
  RecoverDataLoopActivationRequest,
  RecoverDataLoopActivationResponse,
  RollbackProductionBundleRequest,
  RollbackProductionBundleResponse,
  StartDataLoopRequest,
  StartDataLoopResponse,
  SubmitDataLoopDecisionRequest,
  SubmitFeedbackLabelRequest,
} from '@/api/types'

const BASE = '/api/v1/data-loop'

export interface ListFeedbackCasesQuery {
  status?: string[]
  limit?: number
}

/**
 * Data Loop 接口。
 *
 * 该组端点在后端由 `get_data_loop_principal` 保护，要求可信网关注入
 * Authorization、X-Tenant-ID、X-User-ID、X-Data-Loop-Roles 四个头。
 * 前端**绝不**持有网关共享 Token —— 这是当前单文件控制台最主要的安全债。
 */
export const dataLoopApi = {
  startRun(body: StartDataLoopRequest) {
    return request<StartDataLoopResponse>(`${BASE}/runs`, { method: 'POST', body })
  },

  snapshot(workflowId: string) {
    return request<DataLoopSnapshotResponse>(`${BASE}/runs/${workflowId}`)
  },

  decideRun(workflowId: string, body: SubmitDataLoopDecisionRequest) {
    return request<DataLoopDecisionResponse>(`${BASE}/runs/${workflowId}/decision`, {
      method: 'POST',
      body,
    })
  },

  recoverActivation(workflowId: string, body: RecoverDataLoopActivationRequest) {
    return request<RecoverDataLoopActivationResponse>(
      `${BASE}/runs/${workflowId}/activation/recover`,
      { method: 'POST', body },
    )
  },

  recordOperatorDecision(body: RecordOperatorDecisionRequest) {
    return request<OperatorDecisionResponse>(`${BASE}/operator-decisions`, {
      method: 'POST',
      body,
    })
  },

  listFeedbackCases(query: ListFeedbackCasesQuery = {}) {
    return request<FeedbackCaseListResponse>(`${BASE}/feedback-cases`, {
      query: { status: query.status?.length ? query.status : undefined, limit: query.limit },
    })
  },

  submitLabel(feedbackCaseId: string, body: SubmitFeedbackLabelRequest) {
    return request<FeedbackLabelResponse>(`${BASE}/feedback-cases/${feedbackCaseId}/labels`, {
      method: 'POST',
      body,
    })
  },

  listLabels(feedbackCaseId: string) {
    return request<FeedbackLabelListResponse>(`${BASE}/feedback-cases/${feedbackCaseId}/labels`)
  },

  approveLabel(feedbackCaseId: string, labelId: string, body: ApproveFeedbackLabelRequest) {
    return request<FeedbackLabelResponse>(
      `${BASE}/feedback-cases/${feedbackCaseId}/labels/${labelId}/approve`,
      { method: 'POST', body },
    )
  },

  freezeDataset(body: FreezeDatasetRequest) {
    return request<FreezeDatasetResponse>(`${BASE}/datasets/freeze`, { method: 'POST', body })
  },

  getDataset(datasetId: string) {
    return request<Record<string, unknown>>(`${BASE}/datasets/${datasetId}`)
  },

  bootstrapBundle(body: BootstrapProductionBundleRequest) {
    return request<ProductionBundleResponse>(`${BASE}/production-bundles/bootstrap`, {
      method: 'POST',
      body,
    })
  },

  activeBundle() {
    return request<ProductionBundleResponse>(`${BASE}/production-bundles/active`)
  },

  rollbackBundle(body: RollbackProductionBundleRequest) {
    return request<RollbackProductionBundleResponse>(`${BASE}/production-bundles/rollback`, {
      method: 'POST',
      body,
    })
  },

  proposeCandidate(body: ProposeCandidateRequest) {
    return request<CandidateResponse>(`${BASE}/configuration-candidates`, {
      method: 'POST',
      body,
    })
  },

  getCandidate(candidateId: string) {
    return request<CandidateResponse>(`${BASE}/configuration-candidates/${candidateId}`)
  },
}
