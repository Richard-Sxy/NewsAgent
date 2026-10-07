import { defineStore } from 'pinia'
import { ref } from 'vue'

export interface ConfirmOptions {
  title: string
  description?: string
  confirmText?: string
  cancelText?: string
  danger?: boolean
}

/**
 * 归一化后的对话框状态。
 * `options` 始终存在（有默认值），模板里不必处理 null 分支。
 */
export interface ConfirmDialogState {
  title: string
  description: string
  confirmText: string
  cancelText: string
  danger: boolean
}

const INITIAL_STATE: ConfirmDialogState = {
  title: '',
  description: '',
  confirmText: '确认',
  cancelText: '取消',
  danger: false,
}

/**
 * Promise 式二次确认。
 *
 * 用法：`if (!(await dialog.ask({ title: '回滚生产包？', danger: true }))) return`
 * 破坏性操作（取消任务、回滚生产包、激活恢复）一律先过这里。
 */
export const useConfirmStore = defineStore('confirm', () => {
  const open = ref(false)
  const options = ref<ConfirmDialogState>({ ...INITIAL_STATE })
  let resolver: ((value: boolean) => void) | null = null

  function ask(next: ConfirmOptions): Promise<boolean> {
    // 上一个对话框若未settle，直接按取消处理，避免 promise 泄漏。
    resolver?.(false)
    return new Promise<boolean>((resolve) => {
      resolver = resolve
      options.value = {
        title: next.title,
        description: next.description ?? '',
        confirmText: next.confirmText ?? INITIAL_STATE.confirmText,
        cancelText: next.cancelText ?? INITIAL_STATE.cancelText,
        danger: next.danger ?? false,
      }
      open.value = true
    })
  }

  function settle(value: boolean): void {
    open.value = false
    const resolve = resolver
    resolver = null
    resolve?.(value)
  }

  function confirm(): void {
    settle(true)
  }

  function cancel(): void {
    settle(false)
  }

  return { open, options, ask, settle, confirm, cancel }
})
