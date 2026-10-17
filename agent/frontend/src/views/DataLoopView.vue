<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'

const route = useRoute()

const TABS = [
  { name: 'data-loop-feedback', label: '反馈案例', description: '收集问题与人工反馈，完成标注和复核。' },
  { name: 'data-loop-runs', label: '评测运行', description: '查看评测结果，对比当前配置与候选配置。' },
  { name: 'data-loop-decisions', label: '运营决策', description: '追溯热点运营判断与关联反馈。' },
  { name: 'data-loop-datasets', label: '评估数据集', description: '维护评测样本，检查数据集分层与覆盖。' },
  { name: 'data-loop-bundles', label: '生产包', description: '查看生产配置版本，按审批流程激活或回滚。' },
  { name: 'data-loop-candidates', label: '配置候选', description: '审核改进建议，结合评测结果批准或拒绝。' },
] as const

const activeTab = computed(() => TABS.find((tab) => tab.name === route.name) ?? TABS[0])
</script>

<template>
  <header class="na-page-head">
    <div>
      <p class="na-eyebrow">持续改进</p>
      <h1>评测与反馈</h1>
      <p class="na-subtitle">{{ activeTab.description }}</p>
    </div>
    <span class="dataloop-stage">{{ activeTab.label }}</span>
  </header>

  <nav class="dataloop-nav" aria-label="评测与反馈功能">
    <RouterLink v-for="tab in TABS" :key="tab.name" class="dataloop-tab" :to="{ name: tab.name }">
      {{ tab.label }}
    </RouterLink>
  </nav>

  <RouterView />
</template>

<style scoped>
.dataloop-stage {
  padding: 5px 11px;
  border: 1px solid var(--na-border);
  border-radius: 7px;
  background: var(--na-surface);
  color: var(--na-text-muted);
  font-size: 12px;
  white-space: nowrap;
}

.dataloop-nav {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 4px;
  padding: 5px;
  border: 1px solid var(--na-border);
  border-radius: 12px;
  background: var(--na-surface);
}

.dataloop-tab {
  padding: 10px 8px;
  border-radius: 8px;
  color: var(--na-text-muted);
  text-align: center;
  text-decoration: none;
  font-size: 13px;
  transition: background 150ms ease, color 150ms ease;
}

.dataloop-tab:hover {
  background: var(--na-bg);
  color: var(--na-text);
}

.dataloop-tab.router-link-active {
  background: var(--na-accent-weak);
  color: var(--na-accent);
  font-weight: 600;
}

@media (max-width: 700px) {
  .dataloop-nav {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .dataloop-stage {
    display: none;
  }
}
</style>
