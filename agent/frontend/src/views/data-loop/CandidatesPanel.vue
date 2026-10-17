<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { dataLoopApi } from '@/api'
import type { CandidateResponse, JsonObject, ProposeCandidateRequest } from '@/api'
import NaJsonBlock from '@/components/NaJsonBlock.vue'
import { useAsyncTask } from '@/composables/useAsyncTask'
import { useConfirmStore } from '@/stores/confirm'
import { useToastStore } from '@/stores/toast'
import { describeError } from '@/utils/errors'
import { writingJobId } from '@/utils/capabilityReceipts'
import { newIdempotencyKey } from '@/utils/format'
import { firstError, jsonText, parseJson, requiredText } from '@/utils/validate'

const toast = useToastStore()
const route = useRoute()
const dialog = useConfirmStore()
const { pending, run } = useAsyncTask()

const baseBundleId = ref('')
const candidateVersion = ref('')
const proposedSpec = ref('')
const structuredDiff = ref('')
const proposalReason = ref('')

const createResult = ref<CandidateResponse | null>(null)

async function submitProposal(): Promise<void> {
  const problem = firstError(
    requiredText(baseBundleId.value, '基线生产包 ID'),
    requiredText(candidateVersion.value, '候选版本'),
    requiredText(proposalReason.value, '提案原因'),
    jsonText(proposedSpec.value, '候选规格'),
    jsonText(structuredDiff.value, '结构化差异', { expectArray: true }),
  )
  if (problem) {
    toast.warn(problem)
    return
  }

  const confirmed = await dialog.ask({
    title: '确认提交配置候选？',
    description: `${candidateVersion.value.trim()} 将基于 ${baseBundleId.value.trim()} 生成，后续需经过评测门禁才能生效。`,
    confirmText: '确认提交',
  })
  if (!confirmed) return

  const body: ProposeCandidateRequest = {
    base_bundle_id: baseBundleId.value.trim(),
    candidate_version: candidateVersion.value.trim(),
    proposed_spec: parseJson<JsonObject>(proposedSpec.value, {}),
    structured_diff: parseJson<JsonObject[]>(structuredDiff.value, []),
    proposal_reason: proposalReason.value.trim(),
    idempotency_key: newIdempotencyKey('candidate'),
  }

  const result = await run(() => dataLoopApi.proposeCandidate(body), {
    success: '配置候选已提交',
    failure: '提交配置候选失败',
  })
  if (!result) return
  createResult.value = result
  proposalReason.value = ''
}

const lookupId = ref('')
const lookupResult = ref<CandidateResponse | null>(null)
const currentCandidate = computed(() => {
  if (lookupError.value) return null
  if (lookupId.value) return lookupResult.value?.candidate.id === lookupId.value.trim() ? lookupResult.value.candidate : null
  return createResult.value?.candidate ?? null
})
const statusLabels: Record<string, string> = {
  pending_evaluation: '待评测', evaluation_passed: '评测通过', evaluation_failed: '评测未通过',
  approved: '已批准', rejected: '已驳回', activated: '已激活',
}
const currentStatus = computed(() => {
  const status = currentCandidate.value?.status
  return typeof status === 'string' ? statusLabels[status] ?? '状态待确认' : '状态待确认'
})
const changes = computed(() => {
  const diff = currentCandidate.value?.structured_diff
  return Array.isArray(diff) ? diff.filter((item): item is JsonObject => !!item && typeof item === 'object' && !Array.isArray(item)) : []
})
const assetLabels: Record<string, string> = {
  metric_definition: '指标口径', hot_score_policy: '热度规则', reranker_policy: '重排规则',
  analysis_prompt: '分析 Prompt', model_profile: '模型配置', model: '模型', output_schema: '输出格式',
  validator: '校验规则', memory_resolver_policy: '记忆规则',
}
function assetLabel(value: unknown): string {
  return typeof value === 'string' ? assetLabels[value] ?? value : '配置'
}
const lookupLoading = ref(false)
const lookupError = ref<string | null>(null)
let lookupRequest = 0
let disposed = false

async function lookup(): Promise<void> {
  const id = lookupId.value.trim()
  if (!id) {
    toast.warn('请填写候选 ID')
    return
  }
  lookupLoading.value = true
  const sequence = ++lookupRequest
  lookupError.value = null
  try {
    const response = await dataLoopApi.getCandidate(id)
    if (disposed || sequence !== lookupRequest || lookupId.value.trim() !== id) return
    if (response.candidate?.id !== id) throw new Error('候选响应与链接目标不一致')
    lookupResult.value = response
  } catch (cause) {
    if (!disposed && sequence === lookupRequest) { lookupError.value = describeError(cause); lookupResult.value = null }
  } finally {
    if (!disposed && sequence === lookupRequest) lookupLoading.value = false
  }
}

watch(() => route.query.candidate_id, (value) => {
  if (value === undefined) return
  ++lookupRequest
  lookupResult.value = null
  lookupError.value = null
  lookupLoading.value = false
  lookupId.value = ''
  const id = writingJobId(value)
  if (!id) { lookupError.value = '链接中的候选 ID 不合法。'; return }
  lookupId.value = id
  void lookup()
}, { immediate: true })
onBeforeUnmount(() => { disposed = true; ++lookupRequest })
</script>

<template>
  <section class="na-card">
    <div class="na-card__head">
      <h2>候选配置</h2>
      <span v-if="currentCandidate" class="na-badge">{{ currentStatus }}</span>
    </div>
    <details class="na-disclosure" :open="!lookupId && !currentCandidate">
      <summary>查找候选</summary>
      <div class="na-row">
        <div class="na-field na-field--grow">
          <label for="candidate-lookup">候选编号</label>
          <input id="candidate-lookup" v-model="lookupId" class="na-input" autocomplete="off" @keyup.enter="lookup">
        </div>
        <div class="na-field na-field--action">
          <button class="na-btn" type="button" :disabled="lookupLoading" @click="lookup">{{ lookupLoading ? '查询中…' : '查询' }}</button>
        </div>
      </div>
    </details>
    <p v-if="lookupError" class="na-message na-message--error" role="alert">{{ lookupError }}</p>
    <template v-if="currentCandidate">
      <dl class="na-defs">
        <div><dt>版本</dt><dd>{{ currentCandidate.candidate_version }}</dd></div>
        <div><dt>提案原因</dt><dd>{{ currentCandidate.proposal_reason }}</dd></div>
      </dl>
      <h3>变更内容</h3>
      <ul>
        <li v-for="(change, index) in changes" :key="index">
          {{ assetLabel(change.asset) }}：{{ change.before_version }} → {{ change.after_version }}
          <p v-if="change.reason" class="na-muted">{{ change.reason }}</p>
        </li>
      </ul>
      <p class="na-muted">候选配置需要评测和人工批准后才能生效。</p>
      <details class="na-disclosure">
        <summary>查看完整配置与变更证据</summary>
        <NaJsonBlock :value="lookupResult ?? createResult" label="候选完整详情" :collapsed-height="380" />
      </details>
    </template>
    <p v-else-if="!lookupError" class="na-muted">从对话中的候选卡片进入，或填写编号查询。</p>
  </section>

  <details class="na-card na-disclosure">
    <summary>高级操作：手动提交候选</summary>
    <p class="na-muted">日常测试可在对话中选择已注册的模型或 Prompt 预设。这里保留完整配置入口。</p>
    <div class="na-row">
      <div class="na-field na-field--grow">
        <label for="base-bundle">基线生产包 ID</label>
        <input id="base-bundle" v-model="baseBundleId" class="na-input" autocomplete="off">
      </div>
      <div class="na-field na-field--fixed">
        <label for="candidate-version">候选版本</label>
        <input id="candidate-version" v-model="candidateVersion" class="na-input" autocomplete="off">
      </div>
    </div>

    <div class="na-field">
      <label for="proposal-reason">提案原因</label>
      <input id="proposal-reason" v-model="proposalReason" class="na-input">
    </div>

    <div class="na-field">
      <label for="proposed-spec">候选规格（JSON 对象）</label>
      <textarea
        id="proposed-spec"
        v-model="proposedSpec"
        class="na-textarea"
        placeholder="粘贴 ProductionBundleSpec"
      />
    </div>

    <div class="na-field">
      <label for="structured-diff">结构化差异（JSON 数组）</label>
      <textarea
        id="structured-diff"
        v-model="structuredDiff"
        class="na-textarea"
        placeholder="[{&quot;asset&quot;: &quot;analysis_prompt&quot;, &quot;before_version&quot;: &quot;v1&quot;, &quot;after_version&quot;: &quot;v2&quot;, &quot;reason&quot;: &quot;补充证据要求&quot;}]"
      />
    </div>

    <div class="na-row na-row--end">
      <button class="na-btn na-btn--primary" type="button" :disabled="pending" @click="submitProposal">
        {{ pending ? '提交中…' : '提交候选' }}
      </button>
    </div>

    <details v-if="createResult" class="na-disclosure"><summary>提交结果详情</summary><NaJsonBlock :value="createResult" label="提交结果" /></details>
  </details>
</template>
