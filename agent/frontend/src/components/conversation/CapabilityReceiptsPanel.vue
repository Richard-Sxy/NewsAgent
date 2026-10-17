<script setup lang="ts">
import { computed } from 'vue'

import type { ConversationInput, ConversationToolTrace } from '@/api/conversations'
import CapabilityReceiptCard from '@/components/conversation/CapabilityReceiptCard.vue'
import WritingJobCard from '@/components/conversation/WritingJobCard.vue'
import WritingConfirmationCard from '@/components/conversation/WritingConfirmationCard.vue'
import MemoryCard from '@/components/conversation/MemoryCard.vue'
import FeedbackCaseCard from '@/components/conversation/FeedbackCaseCard.vue'
import EvaluationRunCard from '@/components/conversation/EvaluationRunCard.vue'
import CapabilityConfirmationCard from '@/components/conversation/CapabilityConfirmationCard.vue'
import { toolReceipts } from '@/utils/capabilityReceipts'
import { confirmationInput, confirmationOutcome, writingConfirmation } from '@/utils/writingConfirmation'
import { capabilityConfirmation, capabilityConfirmationInput, capabilityConfirmationOutcome } from '@/utils/capabilityConfirmation'

const props = withDefaults(defineProps<{
  tools: ConversationToolTrace[]
  conversationId?: string
  turnId?: string
  active?: boolean
  writingEnabled?: boolean
  availableTools?: string[]
  inputs?: ConversationInput[]
  originalContent?: string
}>(), { conversationId: '', turnId: '', active: false, writingEnabled: false, availableTools: () => [], inputs: () => [], originalContent: '' })
const emit = defineEmits<{ redescribe: [content: string] }>()
const proposals = computed(() => props.tools.map(writingConfirmation).filter((value) => value !== null))
const outcome = computed(() => confirmationOutcome(props.tools))
const genericProposals = computed(() => props.tools.map(capabilityConfirmation).filter((value) => value !== null))
const receipts = computed(() => props.tools.filter((trace) => !writingConfirmation(trace) && !capabilityConfirmation(trace)).flatMap(toolReceipts))
</script>

<template>
  <div v-if="receipts.length || proposals.length || genericProposals.length" class="na-receipts" aria-label="已保存工具回执">
    <WritingConfirmationCard
      v-for="proposal in proposals"
      :key="proposal.input_request_id"
      :confirmation="proposal"
      :conversation-id="conversationId"
      :turn-id="turnId"
      :active="active"
      :enabled="writingEnabled"
      :outcome="outcome"
      :input-submitted="confirmationInput(inputs, proposal)"
      :original-content="originalContent"
      @redescribe="emit('redescribe', $event)"
    />
    <CapabilityConfirmationCard
      v-for="proposal in genericProposals"
      :key="proposal.input_request_id"
      :confirmation="proposal"
      :conversation-id="conversationId"
      :turn-id="turnId"
      :active="active"
      :enabled="availableTools.includes(proposal.hook)"
      :outcome="capabilityConfirmationOutcome(tools, proposal.hook)"
      :input-submitted="capabilityConfirmationInput(inputs, proposal)"
      :original-content="originalContent"
      @redescribe="emit('redescribe', $event)"
    />
    <template v-for="(item, index) in receipts" :key="`${index}-${item.receipt?.entity_id ?? 'unknown'}`">
      <WritingJobCard v-if="item.receipt?.entity_type === 'writing_job'" :receipt="item.receipt" :snapshot="item.job" :execution="item.execution" />
      <MemoryCard v-else-if="item.receipt?.entity_type === 'memory'" :receipt="item.receipt" :snapshot="item.memory" />
      <FeedbackCaseCard v-else-if="item.receipt?.entity_type === 'feedback_case'" :receipt="item.receipt" :snapshot="item.feedback" />
      <EvaluationRunCard v-else-if="item.receipt?.entity_type === 'evaluation_run'" :receipt="item.receipt" :snapshot="item.run" :execution="item.execution" />
      <CapabilityReceiptCard v-else :receipt="item.raw" />
      <p v-if="item.reason" class="na-muted">{{ item.reason }}</p>
    </template>
  </div>
</template>
