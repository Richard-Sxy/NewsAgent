import { defineStore } from 'pinia'
import { ref } from 'vue'

export type ToastKind = 'ok' | 'info' | 'warning' | 'error'

export interface ToastItem {
  id: number
  kind: ToastKind
  text: string
}

/** 错误留得久一些，避免运营还没看清就消失。 */
const DEFAULT_TTL_MS: Record<ToastKind, number> = {
  ok: 4000,
  info: 4000,
  warning: 7000,
  error: 10000,
}

/**
 * 全局轻提示。
 *
 * 视图层不再各自维护 `message` 字符串 —— 那会导致错误提示散落在每个页面里，
 * 且切页即丢失。统一走这里，配合 `NaToastHost` 渲染。
 */
export const useToastStore = defineStore('toast', () => {
  const items = ref<ToastItem[]>([])
  let sequence = 0

  function dismiss(id: number): void {
    items.value = items.value.filter((item) => item.id !== id)
  }

  function push(kind: ToastKind, text: string, ttlMs?: number): number {
    sequence += 1
    const id = sequence
    items.value = [...items.value, { id, kind, text }]
    const duration = ttlMs ?? DEFAULT_TTL_MS[kind]
    if (duration > 0) {
      window.setTimeout(() => dismiss(id), duration)
    }
    return id
  }

  function clear(): void {
    items.value = []
  }

  function ok(text: string): number {
    return push('ok', text)
  }

  function info(text: string): number {
    return push('info', text)
  }

  function warn(text: string): number {
    return push('warning', text)
  }

  function fail(text: string): number {
    return push('error', text)
  }

  return { items, push, dismiss, clear, ok, info, warn, fail }
})
