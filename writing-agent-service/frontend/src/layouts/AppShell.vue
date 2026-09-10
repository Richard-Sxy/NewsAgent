<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'

import NaConfirmDialog from '@/components/NaConfirmDialog.vue'
import NaToastHost from '@/components/NaToastHost.vue'
import { useAppStore } from '@/stores/app'

const appStore = useAppStore()
const route = useRoute()

const navItems = [
  { path: '/hot-news', label: '热点运营' },
  { path: '/jobs', label: '写作任务' },
  { path: '/data-loop', label: 'Data Loop' },
]

const currentTitle = computed(() => {
  const title = route.meta.title
  return typeof title === 'string' ? title : ''
})

/** Data Loop 拆成了子路由，仅靠 router-link-active 无法高亮父级入口。 */
function isActive(path: string): boolean {
  return route.path === path || route.path.startsWith(`${path}/`)
}
</script>

<template>
  <div class="na-shell">
    <header class="na-header">
      <div class="na-header__brand">
        <h1>{{ appStore.appTitle }}</h1>
        <span class="na-badge">{{ appStore.environment }}</span>
      </div>
      <div class="na-header__meta">
        <span v-if="currentTitle" class="na-muted">{{ currentTitle }}</span>
        <span class="na-badge na-badge--live">网关统一鉴权</span>
      </div>
    </header>

    <nav class="na-nav">
      <RouterLink
        v-for="item in navItems"
        :key="item.path"
        :to="item.path"
        :class="{ 'is-active': isActive(item.path) }"
      >
        {{ item.label }}
      </RouterLink>
    </nav>

    <main class="na-main">
      <div v-if="appStore.unauthorized" class="na-banner">
        当前会话未通过网关鉴权或权限不足。请在网关侧重新登录后刷新页面。
      </div>
      <RouterView />
    </main>

    <NaToastHost />
    <NaConfirmDialog />
  </div>
</template>
