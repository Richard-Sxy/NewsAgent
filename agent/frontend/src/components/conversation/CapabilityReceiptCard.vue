<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'

import { formatDateTime } from '@/utils/format'
import { gateLabel, parseCapabilityReceipt, receiptLabel, safeReceiptLink } from '@/utils/capabilityReceipts'

const props = defineProps<{ receipt: unknown; stateShown?: boolean }>()
const parsed = computed(() => parseCapabilityReceipt(props.receipt))
const link = computed(() => parsed.value ? safeReceiptLink(parsed.value) : null)
const title = computed(() => ({ hot_news_run: '热点运行', writing_job: '写作任务', feedback_case: '反馈案例', memory: '记忆',
  candidate: '配置候选', evaluation_run: '评测运行' } as Record<string, string>)[parsed.value?.entity_type ?? ''] ?? '工具回执')
const rawText = computed(() => JSON.stringify(props.receipt ?? null, null, 2) ?? 'null')
const pending = computed(() => ({ memory_promotion: '长期记忆晋升', candidate_evaluation: '候选评测',
  feedback_label_review: '反馈标注二审', bundle_promotion: '配置审批', promotion: '长期记忆晋升',
} as Record<string, string>)[parsed.value?.pending_gate ?? ''] ?? gateLabel(parsed.value?.pending_gate ?? null))
</script>

<template>
  <section class="na-receipt" :aria-label="`${title}回执`">
    <template v-if="parsed">
      <header class="na-receipt__header">
        <strong>{{ title }}</strong>
        <span class="na-receipt__badge" :class="`na-receipt__badge--${parsed.status}`">{{ parsed.status === 'not_landed' ? stateShown ? '请求记录' : '未完成' : '已保存' }}</span>
      </header>
      <slot />
      <p v-if="parsed.pending_gate && !stateShown" class="na-receipt__notice">记录时待人工处理：{{ pending }}。请在详情中核对当前状态。</p>
      <p v-if="parsed.entity_type === 'candidate' && parsed.status !== 'not_landed'" class="na-receipt__notice">候选记录已保存。评测、人工审批和激活请在评审台查看。</p>
      <RouterLink v-if="link" class="na-receipt__link" :to="link">{{ parsed.entity_type === 'writing_job' ? '打开写作任务详情' : '查看详情' }} →</RouterLink>
      <details class="na-receipt__details">
        <summary>记录信息</summary>
        <dl class="na-receipt__meta">
          <div><dt>当时回执</dt><dd>{{ receiptLabel(parsed.status) }}</dd></div>
          <div v-if="parsed.entity_id"><dt>实体 ID</dt><dd class="na-mono">{{ parsed.entity_id }}</dd></div>
          <div v-if="parsed.version"><dt>回执版本</dt><dd>{{ parsed.version }}</dd></div>
          <div><dt>回执时间</dt><dd>{{ formatDateTime(parsed.created_at) }}</dd></div>
        </dl>
      </details>
    </template>
    <template v-else>
      <header><strong>工具回执 · 只读数据</strong></header>
      <p class="na-muted">此回执类型或格式暂不支持。</p>
      <details class="na-receipt__details"><summary>原始记录</summary><pre class="na-receipt__json">{{ rawText }}</pre></details>
    </template>
  </section>
</template>

<style scoped>
.na-receipt { margin-top: 12px; padding: 14px 16px; border: 1px solid var(--na-border, #ddd); border-radius: 12px; background: var(--na-panel, #fff); }
.na-receipt__header { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
.na-receipt__badge { border-radius: 14px; padding: 3px 9px; font-size: 12px; background: #f2f4f7; color: #475467; }
.na-receipt__badge--landed { background: #e8f6ee; color: #17633a; }
.na-receipt__badge--pending_approval { background: #fff4d6; color: #805c00; }
.na-receipt__meta { display: flex; gap: 12px 24px; flex-wrap: wrap; font-size: 12px; }
.na-receipt__details { margin-top: 12px; font-size: 12px; color: var(--na-text-muted, #667085); }
.na-receipt__details summary { cursor: pointer; width: fit-content; }
.na-receipt__notice { margin: 10px 0 0; font-size: 13px; }
dt { color: var(--na-text-muted, #667085); }
dd { margin: 4px 0 0; overflow-wrap: anywhere; }
.na-receipt__link { display: inline-block; margin-top: 12px; color: var(--na-accent); }
.na-receipt__json { max-height: 260px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; }
</style>
