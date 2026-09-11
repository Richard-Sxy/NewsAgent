/**
 * 后端 API 契约的前端镜像。
 *
 * 这是**人工维护的稳定子集**，只覆盖控制台当前用到的字段。
 * 后端契约是唯一事实来源；字段变更后请执行：
 *
 *     npm run gen:api
 *
 * 生成的 `src/api/schema.d.ts` 提供全量权威类型，可逐步替换本文件。
 * 标注 `TODO(gen:api)` 的类型表示其内部结构尚未在前端展开，
 * 刻意保留宽松类型，避免臆造字段。
 */

export type JsonObject = Record<string, unknown>

/* ==================== 写作任务域 ==================== */

export type JobStatus =
  | 'created'
  | 'researching'
  | 'research_review'
  | 'research_completed'
  | 'outlining'
  | 'outline_review'
  | 'drafting'
  | 'assembling'
  | 'reviewing'
  | 'revising'
  | 'final_review'
  | 'waiting_human'
  | 'final_approved'
  | 'published'
  | 'failed'
  | 'cancelled'

export type StepType =
  'research' | 'outline' | 'section_draft' | 'assemble' | 'review' | 'section_revise' | 'finalize'

export type JobScenario = 'research_package' | 'assisted_writing'

export interface WritingJob {
  id: string
  tenant_id: string
  topic: string
  requirements: JsonObject
  status: JobStatus
  current_step: StepType | null
  temporal_workflow_id: string
  scenario: JobScenario
  research_retries: number
  review_rounds: number
  progress_percent: number
  sections_completed: number
  sections_total: number
  created_at: string
  updated_at: string
}

export interface WritingJobList {
  items: WritingJob[]
  total: number
  limit: number
  offset: number
}

export interface CreateWritingJobRequest {
  topic: string
  scenario?: JobScenario
  requirements?: JsonObject
  /** 幂等键，长度 8-128，由前端生成并持久化到本次提交。 */
  idempotency_key: string
}

export interface ListWritingJobsQuery {
  status?: JobStatus[]
  waiting_human_only?: boolean
  query?: string
  limit?: number
  offset?: number
}

export type DecisionGate = 'research' | 'outline' | 'review' | 'final'
export type DecisionAction = 'approve' | 'revise' | 'research' | 'cancel'

export interface HumanDecisionRequest {
  gate: DecisionGate
  action: DecisionAction
  instruction?: string | null
}

export interface DecisionAcceptedResponse {
  accepted: boolean
  job_id: string
  gate: string
}

export interface WorkflowSnapshot {
  phase: string
  completed_steps: number
  review_round: number
  waiting_gate: string | null
  last_artifact_uri: string | null
}

export interface WorkflowProgressResponse {
  job_id: string
  status: JobStatus
  current_step: StepType | null
  workflow: WorkflowSnapshot | null
}

/** TODO(gen:api): app/schemas/research.py::ResearchMetrics 字段较多，按需展开。 */
export type ResearchMetrics = JsonObject

export interface ResearchMetricsResponse {
  job_id: string
  logical_key: string
  artifact_version: number
  metrics: ResearchMetrics
}

/** TODO(gen:api): app/schemas/research.py::ResearchPackage 由 gen:api 生成强类型。 */
export type ResearchPackage = JsonObject

/** TODO(gen:api): app/schemas/checkpoint.py::ResumePoint。 */
export type ResumePoint = JsonObject

export interface RecoveryPlanResponse {
  job_id: string
  can_resume: boolean
  requires_human: boolean
  reason: string
  resume_point: ResumePoint
}

export interface ResumeJobRequest {
  resume_token: string
  recovery_action?: 'research' | null
  instruction?: string | null
}

export interface ResumeJobResponse {
  accepted: boolean
  job_id: string
  workflow_id: string
  status: JobStatus
}

export interface PublishJobRequest {
  channel?: string
}

export interface PublishJobResponse {
  job_id: string
  status: JobStatus
  external_publication_id: string
  published_at: string
  idempotent_replay: boolean
}

/* ==================== SSE 进度事件协议 ==================== */

export type ProgressEventName =
  | 'job.created'
  | 'step.started'
  | 'step.completed'
  | 'step.failed'
  | 'job.waiting_human'
  | 'job.resumed'
  | 'job.completed'
  | 'job.cancelled'
  | 'job.failed'
  | 'job.published'

export interface ProgressValue {
  completed: number
  total: number
  percent: number
}

export interface EventArtifactReference {
  artifact_id: string | null
  logical_key: string
  version: number
}

/** 事件只携带产物引用，不携带文章正文。 */
export interface ProgressEvent {
  event: ProgressEventName
  event_id: string | null
  deduplication_key: string
  tenant_id: string
  job_id: string
  step: StepType | null
  section_id: string | null
  status: JobStatus
  progress: ProgressValue
  artifact: EventArtifactReference | null
  message: string | null
  occurred_at: string
}

/* ==================== Data Loop 域 ==================== */

export type DecisionType = 'accepted' | 'rejected' | 'deferred' | 'corrected'

export type FeedbackSource =
  | 'operator_rejected'
  | 'operator_corrected'
  | 'validation_failed'
  | 'low_confidence'
  | 'retrieval_error'
  | 'post_publish_outcome'

export type FeedbackStatus = 'collected' | 'needs_label' | 'labeled' | 'excluded' | 'frozen'

export type FeedbackProblemType =
  | 'analysis_incorrect'
  | 'unsupported_claim'
  | 'metric_mismatch'
  | 'evidence_mismatch'
  | 'missing_evidence'
  | 'low_confidence'
  | 'retrieval_miss'
  | 'retrieval_false_positive'
  | 'schema_violation'
  | 'policy_violation'
  | 'outcome_underperformance'
  | 'other'

export type FeedbackSeverity = 'low' | 'medium' | 'high' | 'critical'

/** StartDataLoopRequest.window_start / window_end 必须是带时区的 ISO 8601。 */
export interface StartDataLoopRequest {
  window_start: string
  window_end: string
  dataset_name: string
  dataset_version: string
  golden_dataset_id: string
  high_risk_regression_dataset_id: string
  candidate_id: string
  previous_experiment_candidate_id?: string | null
  evaluation_policy_version: string
  idempotency_key: string
}

export interface StartDataLoopResponse {
  workflow_id: string
  status: 'accepted'
}

export interface DataLoopSnapshotResponse {
  workflow_id: string
  phase: string
  dataset_id: string | null
  evaluation_run_id: string | null
  gate_passed: boolean | null
  waiting_for_approval: boolean
}

export interface SubmitDataLoopDecisionRequest {
  action: 'approve' | 'reject'
  reason: string
  idempotency_key: string
}

export interface DataLoopDecisionResponse {
  workflow_id: string
  status: 'submitted'
  created: boolean
}

export interface RecoverDataLoopActivationRequest {
  idempotency_key: string
}

export interface RecoverDataLoopActivationResponse {
  source_workflow_id: string
  recovery_workflow_id: string
  status: 'accepted'
}

export interface RecordOperatorDecisionRequest {
  run_id: string
  news_id: string
  decision_type: DecisionType
  reason: string
  /** 仅 decision_type === 'corrected' 时允许非空，后端会校验。 */
  correction_payload?: JsonObject
  idempotency_key: string
  supersedes_decision_id?: string | null
  feedback_problem_type?: FeedbackProblemType
  feedback_severity?: FeedbackSeverity
}

export interface OperatorDecisionResponse {
  decision_id: string
  decision_type: DecisionType
  created: boolean
}

/**
 * `AnalysisFeedbackCase` 的前端投影。
 * TODO(gen:api): analysis_input_snapshot / source_reference 是深层嵌套结构，
 * 需要展示时再按实际字段展开为强类型。
 */
export interface FeedbackCase {
  id: string
  tenant_id: string
  status: FeedbackStatus
  run_id: string | null
  news_id: string
  source_type: FeedbackSource
  problem_type: FeedbackProblemType
  severity: FeedbackSeverity
  production_bundle_version: string
  occurred_at: string
  recorded_at: string
  content_sha256: string
  idempotency_key: string
  analysis_input_snapshot: JsonObject
  analysis_output_snapshot: JsonObject | null
  source_reference: JsonObject
}

export interface FeedbackCaseListResponse {
  cases: FeedbackCase[]
}

/**
 * TODO(gen:api): `AnalysisFeedbackLabelContent` 的字段由后端定义。
 * 这里保留宽松类型，避免前端臆造标签字段；接入前请先跑 gen:api。
 */
export type FeedbackLabelContent = JsonObject

export type SubmitFeedbackLabelRequest = FeedbackLabelContent & {
  expected_previous_version?: number | null
  idempotency_key: string
}

export interface ApproveFeedbackLabelRequest {
  expected_label_version: number
  idempotency_key: string
}

/** 与 app/schemas/data_loop_api.py::ReviewFeedbackLabelRequest 保持一致；退回时 reason 必填。 */
export interface ReviewFeedbackLabelRequest {
  action: 'approve' | 'request_changes'
  expected_label_version: number
  reason: string
  idempotency_key: string
}

export interface FeedbackLabelResponse {
  /** TODO(gen:api): AnalysisFeedbackLabel */
  label: JsonObject
  created: boolean
}

export interface FeedbackLabelListResponse {
  /** TODO(gen:api): AnalysisFeedbackLabel[] */
  labels: JsonObject[]
}

/** 与 app/schemas/evaluation_dataset.py::EvaluationDatasetLayer 的 Literal 保持一致。 */
export type EvaluationDatasetLayer = 'golden' | 'fresh_bad_case' | 'high_risk_regression'

export interface FreezeDatasetRequest {
  dataset_name: string
  dataset_version: string
  dataset_layer: EvaluationDatasetLayer
  description: string
  feedback_case_ids: string[]
  source_cutoff_at: string
  idempotency_key: string
}

export interface FreezeDatasetResponse {
  /** TODO(gen:api): EvaluationDatasetReference */
  dataset: JsonObject
  created: boolean
}

export interface BootstrapProductionBundleRequest {
  bundle_version: string
  /** TODO(gen:api): ProductionBundleSpec */
  spec: JsonObject
}

export interface ProductionBundleResponse {
  /** TODO(gen:api): ProductionBundle */
  bundle: JsonObject
  created: boolean
}

export interface RollbackProductionBundleRequest {
  target_bundle_id: string
  reason: string
  idempotency_key: string
}

export interface RollbackProductionBundleResponse {
  /** TODO(gen:api): ProductionBundle */
  active_bundle: JsonObject
  /** TODO(gen:api): PromotionDecision */
  decision: JsonObject
  created: boolean
}

export interface ProposeCandidateRequest {
  base_bundle_id: string
  candidate_version: string
  /** TODO(gen:api): ProductionBundleSpec */
  proposed_spec: JsonObject
  /** TODO(gen:api): ConfigurationDiff[] */
  structured_diff: JsonObject[]
  proposal_reason: string
  idempotency_key: string
}

export interface CandidateResponse {
  /** TODO(gen:api): ConfigurationCandidate */
  candidate: JsonObject
  created: boolean
}

/* ---------------------------------------------------------------------- */
/* 热点运营控制台（/api/v1/hot-news）                                       */
/* 与后端 app/schemas/hot_news_api.py 逐字段对齐。                          */
/* 数字（ctr、热度分数与分量）按字符串透传，前端不做二次计算。               */
/* ---------------------------------------------------------------------- */

export interface HotNewsRunSummary {
  run_id: string
  idempotency_key: string
  window_start: string
  window_end: string
  production_bundle_version: string
  workflow_version: string
  status: string
  fetched_record_count: number
  metric_snapshot_count: number
  ranked_news_count: number
  analyzed_news_count: number
  completed_at: string
}

export interface HotNewsRunListResponse {
  runs: HotNewsRunSummary[]
  offset: number
  limit: number
}

export interface HotNewsMetricSnapshotView {
  news_id: string
  content_type: string
  window_start: string
  window_end: string
  impressions: number
  clicks: number
  unique_users: number
  total_duration_seconds: number
  effective_consumptions: number
  interactions: number
  /** 精确小数的字符串形式 */
  ctr: string
}

export interface HotScoreView {
  score: string
  click_component: string
  consumption_component: string
  interaction_component: string
  growth_component: string
}

export interface HotNewsAnalysisSummaryView {
  trend_assessment: string
  dominant_driver: string
  attention_reasons: JsonObject[]
  operation_suggestions: JsonObject[]
  limitations: string[]
  evidence_news_ids: string[]
  overall_confidence: number
  fastgpt_request_id: string | null
  validated_at: string
}

export interface HotNewsRankedItemView {
  rank: number
  news_id: string
  metrics: HotNewsMetricSnapshotView
  baseline: JsonObject | null
  hot_score: HotScoreView
  analysis: HotNewsAnalysisSummaryView | null
}

export interface HotNewsDecisionView {
  decision_id: string
  news_id: string
  decision_type: string
  reason: string
  correction_payload: JsonObject
  operator_id: string
  idempotency_key: string
  supersedes_decision_id: string | null
  created_at: string
}

export interface HotNewsRunDetailResponse {
  run: HotNewsRunSummary
  ranked_news: HotNewsRankedItemView[]
  decisions: HotNewsDecisionView[]
}

export interface RecordHotNewsDecisionRequest {
  run_id: string
  news_id: string
  decision_type: DecisionType
  reason: string
  correction_payload: JsonObject
  idempotency_key: string
  supersedes_decision_id: string | null
  feedback_problem_type: FeedbackProblemType
  feedback_severity: FeedbackSeverity
}

export interface RecordHotNewsDecisionResponse {
  decision_id: string
  decision_type: string
  status: string
  created: boolean
}
