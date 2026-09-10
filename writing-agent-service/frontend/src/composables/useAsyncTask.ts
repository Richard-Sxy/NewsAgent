import { ref } from 'vue'

import { useToastStore } from '@/stores/toast'
import { describeError } from '@/utils/errors'

export interface AsyncTaskOptions {
  /** 成功时弹出的提示文案，留空则不提示。 */
  success?: string
  /** 失败提示的前缀，最终文案为「前缀：具体原因」。 */
  failure?: string
  /** 置为 true 时只写 `error`，不弹提示，适合轮询类调用。 */
  silent?: boolean
}

/**
 * 统一的异步动作包装。
 *
 * 解决三个反复出现的坑：忘记复位 loading、失败后只 console 不提示、
 * 每个页面各写一套 try/catch 文案。返回值在失败时为 `undefined`，
 * 调用方据此提前返回即可。
 */
export function useAsyncTask() {
  const toast = useToastStore()
  const pending = ref(false)
  const error = ref<string | null>(null)

  async function run<T>(
    task: () => Promise<T>,
    options: AsyncTaskOptions = {},
  ): Promise<T | undefined> {
    pending.value = true
    error.value = null
    try {
      const result = await task()
      if (options.success) toast.ok(options.success)
      return result
    } catch (cause) {
      const text = describeError(cause)
      error.value = text
      if (!options.silent) {
        toast.fail(options.failure ? `${options.failure}：${text}` : text)
      }
      return undefined
    } finally {
      pending.value = false
    }
  }

  return { pending, error, run }
}
