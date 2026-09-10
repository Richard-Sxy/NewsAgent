import type {
  DecisionType,
  FeedbackProblemType,
  FeedbackSeverity,
  FeedbackSource,
  FeedbackStatus,
  JobStatus,
  ProgressEventName,
  StepType,
} from '@/api'

export const JOB_STATUS_LABELS: Record<JobStatus, string> = {
  created: '已创建',
  researching: '检索中',
  research_review: '资料审核',
  research_completed: '资料已完成',
  outlining: '大纲生成中',
  outline_review: '大纲审核',
  drafting: '撰写中',
  assembling: '组装中',
  reviewing: '评审中',
  revising: '修订中',
  final_review: '终审中',
  waiting_human: '待人工处理',
  final_approved: '终审通过',
  published: '已发布',
  failed: '失败',
  cancelled: '已取消',
}

export const STEP_LABELS: Record<StepType, string> = {
  research: '检索',
  outline: '大纲',
  section_draft: '章节撰写',
  assemble: '组装',
  review: '评审',
  section_revise: '章节修订',
  finalize: '终稿',
}

export const FEEDBACK_STATUS_LABELS: Record<FeedbackStatus, string> = {
  collected: '已收集',
  needs_label: '待标注',
  labeled: '已标注',
  excluded: '已排除',
  frozen: '已冻结',
}

export const SEVERITY_LABELS: Record<FeedbackSeverity, string> = {
  low: '低',
  medium: '中',
  high: '高',
  critical: '严重',
}

export function jobStatusLabel(status: JobStatus | string): string {
  return JOB_STATUS_LABELS[status as JobStatus] ?? status
}

export function stepLabel(step: StepType | null | undefined): string {
  if (!step) return '-'
  return STEP_LABELS[step] ?? step
}

export function feedbackStatusLabel(status: FeedbackStatus | string): string {
  return FEEDBACK_STATUS_LABELS[status as FeedbackStatus] ?? status
}

export function severityLabel(severity: FeedbackSeverity | string): string {
  return SEVERITY_LABELS[severity as FeedbackSeverity] ?? severity
}

export const EVENT_LABELS: Record<ProgressEventName, string> = {
  'job.created': '任务已创建',
  'step.started': '步骤开始',
  'step.completed': '步骤完成',
  'step.failed': '步骤失败',
  'job.waiting_human': '等待人工处理',
  'job.resumed': '任务已恢复',
  'job.completed': '任务完成',
  'job.cancelled': '任务已取消',
  'job.failed': '任务失败',
  'job.published': '已发布',
}

export const FEEDBACK_SOURCE_LABELS: Record<FeedbackSource, string> = {
  operator_rejected: '运营驳回',
  operator_corrected: '运营修正',
  validation_failed: '校验失败',
  low_confidence: '低置信度',
  retrieval_error: '检索异常',
  post_publish_outcome: '发布后效果',
}

export const PROBLEM_TYPE_LABELS: Record<FeedbackProblemType, string> = {
  analysis_incorrect: '分析有误',
  unsupported_claim: '论点缺少支撑',
  metric_mismatch: '指标不一致',
  evidence_mismatch: '证据不一致',
  missing_evidence: '缺少证据',
  low_confidence: '置信度偏低',
  retrieval_miss: '检索漏召',
  retrieval_false_positive: '检索误召',
  schema_violation: '结构不合法',
  policy_violation: '违反策略',
  outcome_underperformance: '效果未达标',
  other: '其他',
}

export const DECISION_TYPE_LABELS: Record<DecisionType, string> = {
  accepted: '采纳',
  rejected: '驳回',
  deferred: '延后',
  corrected: '修正',
}

export function eventLabel(name: string): string {
  return EVENT_LABELS[name as ProgressEventName] ?? name
}

export function feedbackSourceLabel(source: string): string {
  return FEEDBACK_SOURCE_LABELS[source as FeedbackSource] ?? source
}

export function problemTypeLabel(problem: string): string {
  return PROBLEM_TYPE_LABELS[problem as FeedbackProblemType] ?? problem
}

export function decisionTypeLabel(decision: string): string {
  return DECISION_TYPE_LABELS[decision as DecisionType] ?? decision
}

export type BadgeTone = 'neutral' | 'live' | 'danger' | 'sample'

/** 任务状态 → 徽标配色。终态用实心语义色，中间态保持中性，避免整屏花掉。 */
export function jobStatusTone(status: JobStatus | string): BadgeTone {
  switch (status) {
    case 'final_approved':
    case 'published':
      return 'live'
    case 'failed':
    case 'cancelled':
      return 'danger'
    case 'waiting_human':
      return 'sample'
    default:
      return 'neutral'
  }
}

/** 严重度 → 徽标配色。 */
export function severityTone(severity: FeedbackSeverity | string): BadgeTone {
  switch (severity) {
    case 'critical':
    case 'high':
      return 'danger'
    case 'medium':
      return 'sample'
    default:
      return 'neutral'
  }
}

/** 反馈案例状态 → 徽标配色。 */
export function feedbackStatusTone(status: FeedbackStatus | string): BadgeTone {
  switch (status) {
    case 'frozen':
    case 'labeled':
      return 'live'
    case 'excluded':
      return 'danger'
    case 'needs_label':
      return 'sample'
    default:
      return 'neutral'
  }
}

/** 事件名 → 徽标配色，用于事件流列表快速扫读。 */
export function eventTone(name: string): BadgeTone {
  if (name.endsWith('.failed')) return 'danger'
  if (name === 'job.waiting_human') return 'sample'
  if (name === 'job.completed' || name === 'job.published') return 'live'
  return 'neutral'
}

