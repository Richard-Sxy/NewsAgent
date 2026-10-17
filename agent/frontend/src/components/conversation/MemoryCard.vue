<script setup lang="ts">
import CapabilityReceiptCard from '@/components/conversation/CapabilityReceiptCard.vue'
import type { MemorySnapshot } from '@/utils/capabilityReceipts'
import { formatDateTime } from '@/utils/format'

defineProps<{ receipt: unknown; snapshot: MemorySnapshot | null }>()

function memoryStatus(value: string): string {
  return ({ active: '有效', pending: '待人工审批', approved: '已晋升', rejected: '已拒绝', expired: '已过期',
    superseded: '已替换', invalidated: '已作废' } as Record<string, string>)[value] ?? value
}
</script>

<template>
  <CapabilityReceiptCard :receipt="receipt" :state-shown="!!snapshot">
    <template v-if="snapshot">
      <p class="na-memory-content">{{ snapshot.content }}</p>
      <p class="na-memory-status">{{ snapshot.tier === 'short_term' ? '短期记忆' : '长期记忆候选' }} · {{ memoryStatus(snapshot.effective_status ?? snapshot.status) }}</p>
      <p v-if="snapshot.not_promoted" class="na-muted">
        {{ (snapshot.effective_status ?? snapshot.status) === 'pending' ? '已保存为候选，等待人工晋升后才作为长期记忆使用。' : '尚未晋升为长期记忆。' }}
      </p>
      <details v-if="snapshot.expires_at" class="na-memory-details"><summary>有效期</summary><p>{{ formatDateTime(snapshot.expires_at) }}</p></details>
    </template>
  </CapabilityReceiptCard>
</template>

<style scoped>
.na-memory-content { white-space: pre-wrap; overflow-wrap: anywhere; margin: 12px 0; }
.na-memory-status { margin: 8px 0; font-size: 13px; }
.na-memory-details { font-size: 12px; color: var(--na-text-muted, #667085); }
.na-memory-details summary { cursor: pointer; width: fit-content; }
</style>
