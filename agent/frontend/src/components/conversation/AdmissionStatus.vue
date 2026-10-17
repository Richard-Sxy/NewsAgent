<script setup lang="ts">
import { computed } from 'vue'
import type { ExecutionSnapshot } from '@/utils/admission'

const props = defineProps<{ execution: ExecutionSnapshot }>()
const label = computed(() => {
  if (props.execution.workflowAccepted === true) return ['terminal', 'completed', 'failed', 'rejected'].includes(props.execution.admission?.state ?? '') ? '任务已结束' : '任务已启动'
  const state = props.execution.admission?.state
  if (state === 'queued' || state === 'held') return '排队中'
  if (state === 'granted' || state === 'starting') return '正在启动'
  if (state === 'rejected' || state === 'failed') return '启动请求未执行'
  return '启动状态待核对'
})
</script>

<template>
  <div class="na-admission" aria-label="租户任务排队状态">
    <strong>{{ label }}</strong>
    <template v-if="execution.admission">
      <p v-if="execution.admission.queue_position !== null">队列位置：{{ execution.admission.queue_position }}</p>
      <p v-if="execution.admission.bootstrap_complete === false" class="na-muted">正在核对已有任务，请稍候。</p>
      <details>
        <summary>排队详情</summary>
        <p>租户活跃任务：{{ execution.admission.active_count ?? '待核对' }} / 并发预算：{{ execution.admission.budget ?? '待核对' }}</p>
        <p v-if="execution.admission.error_code" class="na-muted">状态代码：{{ execution.admission.error_code }}</p>
      </details>
    </template>
  </div>
</template>

<style scoped>
.na-admission { margin: 10px 0; font-size: 13px; overflow-wrap: anywhere; }
p { margin: 5px 0; }
details { margin-top: 8px; color: var(--na-text-muted, #667085); font-size: 12px; }
summary { cursor: pointer; width: fit-content; }
</style>
