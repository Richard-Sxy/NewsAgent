<script setup lang="ts">
import { RouterLink } from 'vue-router'
</script>

<template>
  <section class="na-card">
    <h2>页面不存在</h2>
    <p class="na-muted">
      请检查地址，或返回
      <RouterLink :to="{ name: 'jobs' }">写作任务工作台</RouterLink>。
    </p>
  </section>
</template>
