<script setup lang="ts">
import { useToastStore } from '@/stores/toast'

const toast = useToastStore()
</script>

<template>
  <div class="na-toasts" role="status" aria-live="polite">
    <div
      v-for="item in toast.items"
      :key="item.id"
      class="na-toast"
      :class="`na-toast--${item.kind}`"
    >
      <span class="na-toast__text">{{ item.text }}</span>
      <button class="na-toast__close" type="button" aria-label="关闭提示" @click="toast.dismiss(item.id)">
        ×
      </button>
    </div>
  </div>
</template>
