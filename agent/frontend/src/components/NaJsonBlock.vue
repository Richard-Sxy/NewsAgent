<script setup lang="ts">
import { computed, ref } from 'vue'

import { useToastStore } from '@/stores/toast'

const props = withDefaults(
  defineProps<{
    value: unknown
    label?: string
    collapsedHeight?: number
  }>(),
  {
    label: '',
    collapsedHeight: 260,
  },
)

const toast = useToastStore()
const expanded = ref(false)

const text = computed(() => JSON.stringify(props.value ?? null, null, 2) ?? 'null')

const bodyStyle = computed(() => ({
  maxHeight: expanded.value ? 'none' : `${props.collapsedHeight}px`,
}))

async function copy(): Promise<void> {
  try {
    await navigator.clipboard.writeText(text.value)
    toast.ok('已复制到剪贴板')
  } catch {
    toast.fail('复制失败：浏览器未授予剪贴板权限，请手动选择文本')
  }
}
</script>

<template>
  <div class="na-json">
    <div class="na-json__bar">
      <span class="na-muted">{{ label || 'JSON' }}</span>
      <div class="na-row">
        <button class="na-btn" type="button" @click="expanded = !expanded">
          {{ expanded ? '收起' : '展开' }}
        </button>
        <button class="na-btn" type="button" @click="copy">复制</button>
      </div>
    </div>
    <pre class="na-log" :style="bodyStyle">{{ text }}</pre>
  </div>
</template>
