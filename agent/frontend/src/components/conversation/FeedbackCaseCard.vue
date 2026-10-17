<script setup lang="ts">
import CapabilityReceiptCard from '@/components/conversation/CapabilityReceiptCard.vue'
import type { FeedbackCaseSnapshot } from '@/utils/capabilityReceipts'
import { feedbackStatusLabel, problemTypeLabel } from '@/utils/labels'

defineProps<{ receipt: unknown; snapshot: FeedbackCaseSnapshot | null }>()
</script>

<template>
  <CapabilityReceiptCard :receipt="receipt" :state-shown="!!snapshot">
    <dl v-if="snapshot" class="na-defs">
      <div><dt>关联新闻</dt><dd>{{ snapshot.news_id }}</dd></div>
      <div><dt>问题类型</dt><dd>{{ problemTypeLabel(snapshot.issue_type) }}</dd></div>
      <div><dt>期望标签草稿</dt><dd>{{ snapshot.expected_label_draft }}</dd></div>
      <div><dt>案例状态</dt><dd>{{ feedbackStatusLabel(snapshot.status) }}</dd></div>
    </dl>
    <p v-if="snapshot" class="na-muted">同时记录本次分析驳回反馈；期望标签仅草稿，后续标注与二审在反馈案例页面完成。</p>
    <details v-if="snapshot" class="na-feedback-details">
      <summary>反馈来源</summary>
      <dl class="na-defs">
        <div><dt>热点运行 ID</dt><dd class="na-mono">{{ snapshot.run_id }}</dd></div>
        <div><dt>生产规则包版本</dt><dd>{{ snapshot.production_bundle_version }}</dd></div>
      </dl>
    </details>
  </CapabilityReceiptCard>
</template>

<style scoped>
.na-defs { font-size: 13px; }
dd { overflow-wrap: anywhere; white-space: pre-wrap; }
.na-feedback-details { font-size: 12px; color: var(--na-text-muted, #667085); }
.na-feedback-details summary { cursor: pointer; width: fit-content; }
</style>
