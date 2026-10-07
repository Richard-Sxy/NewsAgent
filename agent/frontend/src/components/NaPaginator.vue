<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  total: number
  limit: number
  offset: number
}>()

const emit = defineEmits<{ (event: 'change', offset: number): void }>()

const safeLimit = computed(() => Math.max(1, props.limit))
const pageCount = computed(() => Math.max(1, Math.ceil(props.total / safeLimit.value)))
const page = computed(() => Math.floor(props.offset / safeLimit.value) + 1)
const canPrev = computed(() => props.offset > 0)
const canNext = computed(() => props.offset + safeLimit.value < props.total)

function goToPage(target: number): void {
  const clamped = Math.min(Math.max(1, target), pageCount.value)
  const next = (clamped - 1) * safeLimit.value
  if (next !== props.offset) emit('change', next)
}
</script>

<template>
  <div class="na-pager">
    <span class="na-muted">第 {{ page }} / {{ pageCount }} 页 · 共 {{ total }} 条</span>
    <div class="na-pager__actions">
      <button class="na-btn" type="button" :disabled="!canPrev" @click="goToPage(1)">首页</button>
      <button class="na-btn" type="button" :disabled="!canPrev" @click="goToPage(page - 1)">
        上一页
      </button>
      <button class="na-btn" type="button" :disabled="!canNext" @click="goToPage(page + 1)">
        下一页
      </button>
      <button class="na-btn" type="button" :disabled="!canNext" @click="goToPage(pageCount)">
        末页
      </button>
    </div>
  </div>
</template>
