export interface AdmissionSnapshot {
  state: string
  queue_position: number | null
  active_count: number | null
  budget: number | null
  bootstrap_complete: boolean | null
  error_code: string | null
  request_workflow_id: string
}

export interface ExecutionSnapshot {
  admission: AdmissionSnapshot | null
  workflowAccepted: boolean | null
  admissionAccepted: boolean | null
}

const STATES = new Set(['submitted', 'initializing', 'queued', 'granted', 'starting', 'running', 'terminal', 'completed',
  'failed', 'held', 'rejected', 'unknown', 'not_found'])

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}

function count(value: unknown, minimum = 0): boolean {
  return value === null || typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum
}

export function parseAdmission(value: unknown): AdmissionSnapshot | null {
  const item = object(value)
  if (!item || typeof item.state !== 'string' || !STATES.has(item.state) || !count(item.queue_position, 1)
    || !count(item.active_count) || !count(item.budget, 1)
    || !(item.bootstrap_complete === null || typeof item.bootstrap_complete === 'boolean')
    || !(item.error_code === null || typeof item.error_code === 'string' && item.error_code.length <= 128)
    || typeof item.request_workflow_id !== 'string' || !/^conversation-admission-request-[0-9a-f]{64}$/.test(item.request_workflow_id)) return null
  return item as unknown as AdmissionSnapshot
}

export function executionSnapshot(value: unknown): ExecutionSnapshot {
  const item = object(value)
  return { admission: parseAdmission(item?.admission),
    workflowAccepted: typeof item?.workflow_accepted === 'boolean' ? item.workflow_accepted : null,
    admissionAccepted: typeof item?.admission_accepted === 'boolean' ? item.admission_accepted : null }
}

export function admissionTerminal(value: AdmissionSnapshot | null): boolean {
  return !!value && ['terminal', 'completed', 'failed', 'rejected'].includes(value.state)
}

export function executionLabel(execution: ExecutionSnapshot): string {
  const state = execution.admission?.state
  if (execution.workflowAccepted === true) return admissionTerminal(execution.admission) ? '业务 Workflow 已结束' : '业务 Workflow 已启动'
  if (state === 'queued' || state === 'held') return '排队中'
  if (state === 'granted') return '已取得并发名额，等待启动'
  if (state === 'starting') return '正在启动，等待确认'
  if (state === 'rejected' || state === 'failed') return '启动请求未执行'
  return '启动状态待核对'
}
