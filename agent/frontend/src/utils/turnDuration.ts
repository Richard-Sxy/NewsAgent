import type { ConversationTurn } from '@/api/conversations'

export type TurnTimingInput = Pick<ConversationTurn, 'status' | 'created_at' | 'completed_at'>

const TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/

/**
 * 全链路耗时 = 服务端受理问题（created_at）到答复终态落库（completed_at）的时间窗口，
 * 覆盖问题理解、召回/知识检索、工具执行与答复保存；处理中、字段缺失或时间非法时返回 null。
 */
export function turnDurationSeconds(turn: TurnTimingInput): number | null {
  if (turn.status === 'processing') return null
  if (typeof turn.created_at !== 'string' || typeof turn.completed_at !== 'string') return null
  if (!TIMESTAMP.test(turn.created_at) || !TIMESTAMP.test(turn.completed_at)) return null
  const started = Date.parse(turn.created_at)
  const finished = Date.parse(turn.completed_at)
  if (!Number.isFinite(started) || !Number.isFinite(finished) || finished < started) return null
  return (finished - started) / 1000
}

/** 展示用字符串：保留一位小数；输入非法时返回 null，由调用方隐藏整行。 */
export function formatTurnDuration(seconds: number | null): string | null {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return null
  return seconds.toFixed(1)
}
