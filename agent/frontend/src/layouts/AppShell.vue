<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'

import NaConfirmDialog from '@/components/NaConfirmDialog.vue'
import NaToastHost from '@/components/NaToastHost.vue'
import { useAppStore } from '@/stores/app'

const appStore = useAppStore()
const route = useRoute()

const businessNavItems = [
  { path: '/chat', label: '对话', icon: 'M5 5h14v10H9l-4 4V5Z' },
  { path: '/hot-news', label: '热点运营', icon: 'm4 16 5-5 4 3 7-9M15 5h5v5' },
  { path: '/jobs', label: '写作任务', icon: 'M14 3H5v18h14V8l-5-5Zm0 0v5h5M8 12h8M8 16h6' },
  { path: '/data-loop', label: '评测与反馈', icon: 'M20 7a8 8 0 0 0-14-2L3 8m0-5v5h5M4 17a8 8 0 0 0 14 2l3-3m0 5v-5h-5' },
]
const navItems = computed(() => [
  ...(appStore.environment === 'streaming-lab' ? [] : businessNavItems),
  ...(appStore.config?.enableStreamingLab ? [{ path: '/streaming-lab', label: '流式快照', icon: 'M4 6h16M4 12h10M4 18h16m-4-9 4 3-4 3' }] : []),
])

const currentTitle = computed(() => {
  const title = route.meta.title
  return typeof title === 'string' ? title : ''
})
const displayTitle = computed(() => appStore.appTitle.startsWith('NewsAgent ') ? 'NewsAgent' : appStore.appTitle)
const environmentLabel = computed(() => ({ 'streaming-lab': '本地合成实验', e2e: '本地演示', development: '开发环境', test: '测试环境', production: '生产环境' } as Record<string, string>)[appStore.environment] ?? appStore.environment)
const currentModule = computed(() => navItems.value.find((item) => isActive(item.path))?.label ?? (isActive('/security-tests') ? '安全测试' : currentTitle.value))
const breadcrumb = computed(() => route.path.startsWith('/data-loop/') ? currentTitle.value : '')

/** Data Loop 拆成了子路由，仅靠 router-link-active 无法高亮父级入口。 */
function isActive(path: string): boolean {
  return route.path === path || route.path.startsWith(`${path}/`)
}
</script>

<template>
  <div class="na-shell">
    <a class="na-skip-link" href="#main-content">跳到主要内容</a>
    <aside class="na-sidebar" aria-label="工作空间导航">
      <RouterLink to="/chat" class="na-brand" :title="appStore.appTitle">
        <span class="na-brand__mark" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none">
            <path d="M6 17V7l12 10V7" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" />
            <circle cx="18" cy="7" r="2" fill="currentColor" />
          </svg>
        </span>
        <span class="na-brand__text">{{ displayTitle }}<small>新闻运营工作台</small></span>
      </RouterLink>
      <div class="na-sidebar__label">工作空间</div>
      <nav class="na-nav" aria-label="主要功能">
        <RouterLink
          v-for="item in navItems"
          :key="item.path"
          :to="item.path"
          :class="{ 'is-active': isActive(item.path) }"
          :aria-current="isActive(item.path) ? 'page' : undefined"
        >
          <svg class="na-nav__icon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path :d="item.icon" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
          {{ item.label }}
          <span v-if="isActive(item.path)" class="na-nav__dot" aria-hidden="true" />
        </RouterLink>
      </nav>
      <div v-if="appStore.environment !== 'streaming-lab'" class="na-sidebar__footer">
        <RouterLink class="na-utility-link" to="/security-tests" :class="{ 'is-active': isActive('/security-tests') }" :aria-current="isActive('/security-tests') ? 'page' : undefined">
          <svg class="na-nav__icon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Zm-4 9 3 3 5-6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
          安全测试
        </RouterLink>
      </div>
    </aside>

    <div class="na-workspace">
      <header class="na-header">
        <div class="na-header__breadcrumb">
          <span>{{ currentModule }}</span>
          <template v-if="breadcrumb"><span class="na-header__separator" aria-hidden="true">/</span><span class="na-muted">{{ breadcrumb }}</span></template>
        </div>
        <span class="na-environment">{{ environmentLabel }}</span>
      </header>
      <main id="main-content" class="na-main" tabindex="-1">
        <div v-if="appStore.unauthorized" class="na-banner" role="alert">
          当前会话未通过网关鉴权或权限不足。请在网关侧重新登录后刷新页面。
        </div>
        <RouterView />
      </main>
    </div>

    <NaToastHost />
    <NaConfirmDialog />
  </div>
</template>
