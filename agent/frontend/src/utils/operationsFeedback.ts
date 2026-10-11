import type {
  DecisionType,
  FeedbackSeverity,
  JsonObject,
  OperationsFeedback,
  OperationsFeedbackCategory,
  OperationsReasonCode,
} from '@/api/types'
import { OPERATIONS_FEEDBACK_CATEGORY_LABELS, OPERATIONS_REASON_LABELS } from '@/utils/labels'

export const OPERATIONS_FEEDBACK_CATEGORIES = Object.keys(
  OPERATIONS_FEEDBACK_CATEGORY_LABELS,
) as OperationsFeedbackCategory[]

export const OPERATIONS_REASON_CODES = Object.keys(OPERATIONS_REASON_LABELS) as OperationsReasonCode[]

export function defaultOperationsCategory(decision: DecisionType): OperationsFeedbackCategory {
  if (decision === 'accepted') return 'positive_example'
  if (decision === 'deferred') return 'strategy_judgment'
  return 'analysis_error'
}

export function defaultOperationsSeverity(category: OperationsFeedbackCategory): FeedbackSeverity {
  return category === 'analysis_error' ? 'medium' : 'low'
}

/** 兼容没有业务分类的历史来源引用，不从动作推断评测结论。 */
export function readOperationsFeedback(reference: JsonObject): OperationsFeedback | null {
  const value = reference.operator_feedback
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const feedback = value as JsonObject
  if (
    typeof feedback.category !== 'string' ||
    !OPERATIONS_FEEDBACK_CATEGORIES.includes(feedback.category as OperationsFeedbackCategory) ||
    !Array.isArray(feedback.reason_codes) ||
    !feedback.reason_codes.every((code) =>
      typeof code === 'string' && OPERATIONS_REASON_CODES.includes(code as OperationsReasonCode),
    )
  ) return null
  return {
    category: feedback.category as OperationsFeedbackCategory,
    reason_codes: feedback.reason_codes as OperationsReasonCode[],
  }
}
