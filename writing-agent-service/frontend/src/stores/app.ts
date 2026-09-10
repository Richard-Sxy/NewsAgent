import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import type { RuntimeConfig } from '@/config/runtime'

/**
 * 应用级状态：运行时配置镜像 + 未授权广播。
 *
 * 不存在"登录"动作 —— 会话由企业网关维护，前端只消费网关注入的身份。
 */
export const useAppStore = defineStore('app', () => {
  const config = ref<RuntimeConfig | null>(null)
  const unauthorized = ref(false)

  const appTitle = computed(() => config.value?.appTitle ?? 'NewsAgent 运营控制台')
  const environment = computed(() => config.value?.environment ?? 'unknown')
  const hotNewsEnabled = computed(() => config.value?.enableHotNews ?? false)
  const isProduction = computed(() => environment.value === 'production')

  function applyConfig(value: RuntimeConfig): void {
    config.value = value
  }

  function markUnauthorized(): void {
    unauthorized.value = true
  }

  function clearUnauthorized(): void {
    unauthorized.value = false
  }

  return {
    config,
    unauthorized,
    appTitle,
    environment,
    hotNewsEnabled,
    isProduction,
    applyConfig,
    markUnauthorized,
    clearUnauthorized,
  }
})
