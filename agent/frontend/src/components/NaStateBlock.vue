<script setup lang="ts">
/**
 * 载入 / 失败 / 空三态的统一渲染。
 *
 * 视图里不再各写一遍 `v-if="loading"` / `v-if="error"` / `v-if="empty"`，
 * 这是「稳定」的关键之一：任何一个面板都不会出现"什么都不显示"的空白页。
 */
withDefaults(
  defineProps<{
    loading?: boolean
    error?: string | null
    empty?: boolean
    loadingText?: string
    emptyText?: string
  }>(),
  {
    loading: false,
    error: null,
    empty: false,
    loadingText: '加载中…',
    emptyText: '暂无数据',
  },
)
</script>

<template>
  <p v-if="loading" class="na-empty">{{ loadingText }}</p>
  <p v-else-if="error" class="na-message na-message--error na-empty">{{ error }}</p>
  <p v-else-if="empty" class="na-empty">{{ emptyText }}</p>
  <slot v-else />
</template>
