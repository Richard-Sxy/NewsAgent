<script setup lang="ts">
import { useConfirmStore } from '@/stores/confirm'

const dialog = useConfirmStore()
</script>

<template>
  <div v-if="dialog.open" class="na-modal">
    <div class="na-modal__panel" role="dialog" aria-modal="true" :aria-label="dialog.options.title">
      <h3>{{ dialog.options.title }}</h3>
      <p v-if="dialog.options.description" class="na-muted">{{ dialog.options.description }}</p>
      <div class="na-modal__actions">
        <button class="na-btn" type="button" @click="dialog.cancel()">
          {{ dialog.options.cancelText }}
        </button>
        <button
          class="na-btn"
          :class="dialog.options.danger ? 'na-btn--danger' : 'na-btn--primary'"
          type="button"
          @click="dialog.confirm()"
        >
          {{ dialog.options.confirmText }}
        </button>
      </div>
    </div>
  </div>
</template>
