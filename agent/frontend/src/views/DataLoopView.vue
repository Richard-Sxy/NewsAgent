<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'

const TABS = [
  { name: 'data-loop-feedback', label: '反馈案例' },
  { name: 'data-loop-runs', label: '评测运行' },
  { name: 'data-loop-decisions', label: '运营决策' },
  { name: 'data-loop-datasets', label: '评估数据集' },
  { name: 'data-loop-bundles', label: '生产包' },
  { name: 'data-loop-candidates', label: '配置候选' },
] as const
</script>

<template>
  <section class="na-card na-card--tight">
    <div class="na-card__head">
      <h2>Data Loop 评审台</h2>
      <span class="na-badge na-badge--live">真实接口</span>
    </div>
    <p class="na-muted">
      写操作全部要求网关注入的身份头与角色声明；前端不持有网关共享令牌。
      所有破坏性动作（激活恢复、生产包回滚）都会先经过二次确认。
    </p>
  </section>

  <nav class="na-tabs">
    <RouterLink v-for="tab in TABS" :key="tab.name" class="na-tab" :to="{ name: tab.name }">
      {{ tab.label }}
    </RouterLink>
  </nav>

  <RouterView />
</template>
