import type { ConversationToolTrace } from '@/api/conversations'
import type { JobStatus, StepType } from '@/api/types'
import { executionSnapshot } from '@/utils/admission'
import type { ExecutionSnapshot } from '@/utils/admission'

export type ReceiptStatus = 'landed' | 'pending_approval' | 'not_landed'

export interface CapabilityReceipt {
  entity_type: string
  entity_id: string | null
  status: ReceiptStatus
  version: string | null
  deep_link: string | null
  pending_gate: string | null
  created_at: string
}

export interface WritingJobSnapshot {
  id: string
  topic: string
  status: JobStatus
  current_step: StepType | null
  progress_percent: number
  created_at: string
  updated_at: string
}

export interface ToolReceipt {
  raw: unknown
  receipt: CapabilityReceipt | null
  job: WritingJobSnapshot | null
  memory: MemorySnapshot | null
  feedback: FeedbackCaseSnapshot | null
  run: EvaluationSnapshot | null
  reason: string | null
  execution: ExecutionSnapshot
}

export interface EvaluationSnapshot {
  workflow_id: string
  phase: string
  gate_passed: boolean | null
  waiting_for_approval: boolean
  evaluation_run_id: string | null
  dataset_id: string | null
  policy_version: string | null
  terminal: boolean
  admission?: Record<string, unknown> | null
}

export interface MemorySnapshot {
  id: string
  content: string
  expires_at: string | null
  tier: 'short_term' | 'long_term_candidate'
  candidate_status: string | null
  status: string
  effective_status?: string
  version: string | number
  not_promoted: boolean
}

export interface FeedbackCaseSnapshot {
  case_id: string
  run_id: string
  news_id: string
  production_bundle_version: string
  issue_type: string
  expected_label_draft: string
  status: string
}

const ENTITY_TYPES = new Set(['hot_news_run', 'writing_job', 'feedback_case', 'memory', 'candidate', 'evaluation_run'])
const ENTITY_LINKS: Record<string, [string, string]> = {
  hot_news_run: ['/hot-news', 'run_id'], writing_job: ['/jobs', 'job_id'], feedback_case: ['/data-loop', 'case_id'],
  memory: ['/data-loop', 'memory_id'], candidate: ['/data-loop', 'candidate_id'], evaluation_run: ['/data-loop', 'run_id'],
}
const RECEIPT_STATUSES = new Set(['landed', 'pending_approval', 'not_landed'])
const JOB_STATUSES = new Set(['created', 'researching', 'research_review', 'research_completed', 'outlining', 'outline_review',
  'drafting', 'assembling', 'reviewing', 'revising', 'final_review', 'waiting_human', 'final_approved', 'published', 'failed', 'cancelled'])
const STEPS = new Set(['research', 'outline', 'section_draft', 'assemble', 'review', 'section_revise', 'finalize'])
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const TIMESTAMP = /^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}

function timestamp(value: unknown): value is string {
  return typeof value === 'string' && TIMESTAMP.test(value) && Number.isFinite(Date.parse(value))
}

export function writingJobId(value: unknown): string | null {
  return typeof value === 'string' && UUID.test(value) ? value : null
}

export function dataLoopWorkflowId(value: unknown): string | null {
  return typeof value === 'string' && /^hot-news-data-loop-[a-f0-9]{64}$/.test(value) ? value : null
}

function identifier(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 256 && /^[A-Za-z0-9][A-Za-z0-9_.:-]*$/.test(value)
}

/** Only relative console routes are navigable, with the same entity as the receipt. */
export function safeReceiptLink(receipt: CapabilityReceipt): string | null {
  const link = receipt.deep_link
  const expected = ENTITY_LINKS[receipt.entity_type]
  if (!link || link.length > 512 || !expected || !identifier(receipt.entity_id) || /[\\\s#%]/.test(link)
    || [...link].some((character) => character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127)) return null
  if (receipt.entity_type === 'writing_job' && !writingJobId(receipt.entity_id)) return null
  let selector = expected[1]
  if (receipt.entity_type === 'evaluation_run' && receipt.entity_id.startsWith('hot-news-data-loop-')) {
    if (!dataLoopWorkflowId(receipt.entity_id)) return null
    selector = 'workflow_id'
  }
  return link === expected[0] || link === `${expected[0]}?${selector}=${receipt.entity_id}` ? link : null
}

export function parseCapabilityReceipt(value: unknown): CapabilityReceipt | null {
  const item = object(value)
  if (!item || typeof item.entity_type !== 'string' || !ENTITY_TYPES.has(item.entity_type)
    || typeof item.status !== 'string' || !RECEIPT_STATUSES.has(item.status) || !timestamp(item.created_at)
    || !(item.entity_id === null || identifier(item.entity_id))
    || !(item.version === null || typeof item.version === 'string' && item.version.length > 0 && item.version.length <= 128
      && !/\s/.test(item.version) && ![...item.version].some((character) => character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127))
    || !(item.deep_link === null || typeof item.deep_link === 'string')
    || !(item.pending_gate === null || identifier(item.pending_gate))) return null
  if (item.status !== 'not_landed' && (!item.entity_id || !item.version)) return null
  if (item.status === 'not_landed' && (item.entity_id !== null || item.version !== null || item.deep_link !== null || item.pending_gate !== null)) return null
  if (item.status !== 'pending_approval' && item.pending_gate !== null) return null
  if (item.entity_type === 'writing_job' && item.entity_id !== null && !writingJobId(item.entity_id)) return null
  if (item.deep_link !== null && !safeReceiptLink(item as unknown as CapabilityReceipt)) return null
  return item as unknown as CapabilityReceipt
}

export function parseWritingJobSnapshot(value: unknown, expectedId: string): WritingJobSnapshot | null {
  const item = object(value)
  if (!item || item.id !== expectedId || !writingJobId(item.id) || typeof item.topic !== 'string'
    || typeof item.status !== 'string' || !JOB_STATUSES.has(item.status)
    || !(item.current_step === null || typeof item.current_step === 'string' && STEPS.has(item.current_step))
    || typeof item.progress_percent !== 'number' || !Number.isFinite(item.progress_percent)
    || item.progress_percent < 0 || item.progress_percent > 100 || !timestamp(item.updated_at) || !timestamp(item.created_at)) return null
  return item as unknown as WritingJobSnapshot
}

export function parseMemorySnapshot(value: unknown, expectedId: string): MemorySnapshot | null {
  const item = object(value)
  if (!item || item.id !== expectedId || !identifier(item.id) || typeof item.content !== 'string' || item.content.length > 4000
    || !['short_term', 'long_term_candidate'].includes(String(item.tier)) || typeof item.status !== 'string'
    || !(item.expires_at === null || timestamp(item.expires_at))
    || !(item.candidate_status === null || typeof item.candidate_status === 'string')
    || !(typeof item.version === 'string' || typeof item.version === 'number' && Number.isSafeInteger(item.version) && item.version > 0)
    || typeof item.not_promoted !== 'boolean') return null
  if (item.effective_status !== undefined && typeof item.effective_status !== 'string') return null
  return item as unknown as MemorySnapshot
}

export function parseFeedbackSnapshot(value: unknown, expectedId: string): FeedbackCaseSnapshot | null {
  const item = object(value)
  if (!item || item.case_id !== expectedId || !identifier(item.case_id) || !identifier(item.run_id) || !identifier(item.news_id)
    || typeof item.production_bundle_version !== 'string' || typeof item.issue_type !== 'string'
    || typeof item.expected_label_draft !== 'string' || item.expected_label_draft.length > 96 || typeof item.status !== 'string') return null
  return item as unknown as FeedbackCaseSnapshot
}

export function evaluationTerminal(phase: string): boolean {
  return ['evaluation_failed', 'approval_expired', 'rejected', 'activated'].includes(phase)
}

export function parseEvaluationSnapshot(value: unknown, expectedId: string, policyVersion?: string): EvaluationSnapshot | null {
  const item = object(value)
  if (!item || item.workflow_id !== expectedId || !dataLoopWorkflowId(item.workflow_id) || typeof item.phase !== 'string' || !item.phase
    || !(item.gate_passed === null || typeof item.gate_passed === 'boolean') || typeof item.waiting_for_approval !== 'boolean'
    || !(item.evaluation_run_id === null || writingJobId(item.evaluation_run_id))
    || !(item.dataset_id === undefined || item.dataset_id === null || writingJobId(item.dataset_id))
    || !(item.policy_version === undefined || item.policy_version === null || typeof item.policy_version === 'string' && item.policy_version)
    || !(item.terminal === undefined || typeof item.terminal === 'boolean')
    || !(item.admission === undefined || item.admission === null || object(item.admission))) return null
  return { ...item, dataset_id: item.dataset_id ?? null, policy_version: item.policy_version ?? policyVersion ?? null,
    terminal: item.terminal ?? evaluationTerminal(item.phase) } as EvaluationSnapshot
}

/** Saved server receipts are the only source; explicit admission rejection can retain a committed job. */
export function toolReceipts(trace: ConversationToolTrace): ToolReceipt[] {
  if (trace.status !== 'completed' && trace.status !== 'denied') return []
  const result = object(trace.result)
  if (!result) return []
  const raw = Array.isArray(result.receipts) ? result.receipts : Object.hasOwn(result, 'receipt') ? [result.receipt] : []
  const jobs = Array.isArray(result.items) ? result.items : result.job ? [result.job] : []
  const execution = executionSnapshot(result)
  const rejected = execution.admission?.state === 'rejected' && execution.workflowAccepted === false && execution.admissionAccepted === false
  const allowed = trace.status === 'denied' ? raw.filter((value) => {
    const receipt = parseCapabilityReceipt(value)
    if (receipt?.status === 'not_landed') return true
    if (!rejected || trace.name !== 'start_writing_intent' || receipt?.entity_type !== 'writing_job' || receipt.status !== 'landed'
      || !receipt.entity_id || safeReceiptLink(receipt) !== `/jobs?job_id=${receipt.entity_id}`) return false
    const job = object(result.job)
    return job?.version === receipt.version && parseWritingJobSnapshot(job, receipt.entity_id) !== null
  }) : raw
  return allowed.map((value) => {
    const receipt = parseCapabilityReceipt(value)
    const job = receipt?.entity_type === 'writing_job' && receipt.entity_id
      ? jobs.map((item) => parseWritingJobSnapshot(item, receipt.entity_id!)).find(Boolean) ?? null : null
    const memory = receipt?.entity_type === 'memory' && receipt.entity_id ? parseMemorySnapshot(result.memory, receipt.entity_id) : null
    const feedback = receipt?.entity_type === 'feedback_case' && receipt.entity_id ? parseFeedbackSnapshot(result.feedback, receipt.entity_id) : null
    const policyVersion = typeof result.evaluation_policy_version === 'string' ? result.evaluation_policy_version
      : trace.name === 'start_data_loop' ? receipt?.version ?? undefined : undefined
    const queuedId = trace.status === 'completed' && receipt?.entity_type === 'evaluation_run' && receipt.status === 'not_landed'
      && execution.admissionAccepted === true && execution.admission ? dataLoopWorkflowId(object(result.run)?.workflow_id) : null
    const runId = receipt?.entity_type === 'evaluation_run' ? receipt.entity_id ?? queuedId : null
    const run = runId ? parseEvaluationSnapshot(result.run, runId, policyVersion) : null
    const reason = receipt?.status === 'not_landed' && typeof result.reason === 'string' && result.reason.length <= 2000 ? result.reason : null
    return { raw: value, receipt, job, memory, feedback, run, reason, execution }
  })
}

export function writingJobTerminal(snapshot: WritingJobSnapshot): boolean {
  return ['research_completed', 'final_approved', 'published', 'failed', 'cancelled'].includes(snapshot.status)
}

export function gateLabel(gate: string | null): string {
  if (!gate) return '无'
  return ({ research: '研究包', outline: '提纲', review: '评审', final: '终稿', human: '人工节点' } as Record<string, string>)[gate] ?? gate
}

/** Mirror the persisted job-state projection used by the conversation reader. */
export function writingJobGate(snapshot: WritingJobSnapshot): string | null {
  const gate = ({ research_review: 'research', outline_review: 'outline', final_review: 'final' } as Record<string, string>)[snapshot.status]
  if (gate) return gate
  if (snapshot.status === 'waiting_human') {
    const gates: Record<string, string> = { research: 'research', outline: 'outline', review: 'review', finalize: 'final' }
    return gates[snapshot.current_step ?? ''] ?? 'human'
  }
  return null
}

export function receiptLabel(status: ReceiptStatus): string {
  return { landed: '已落库', pending_approval: '已保存 · 待人工审批', not_landed: '未落地' }[status]
}
