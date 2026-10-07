import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from '@/App.vue'
import { router } from '@/router'
import { loadRuntimeConfig } from '@/config/runtime'
import { useAppStore } from '@/stores/app'

import '@/styles/base.css'

/**
 * 启动顺序：先取运行时配置，再挂载路由。
 * 配置来自 /config.json（由 ConfigMap 覆盖），因此镜像不绑定任何环境。
 */
async function bootstrap(): Promise<void> {
  const config = await loadRuntimeConfig()

  const app = createApp(App)
  app.use(createPinia())

  const appStore = useAppStore()
  appStore.applyConfig(config)

  window.addEventListener('newsagent:unauthorized', () => appStore.markUnauthorized())

  app.use(router)
  await router.isReady()
  app.mount('#app')

  if (config.errorReportingDsn) {
    // TODO: 接入企业错误上报（Sentry / 内部平台），此处只保留挂载点。
    console.info('[observability] error reporting DSN configured')
  }
}

void bootstrap()
