<script setup lang="ts">
import { ref } from 'vue'

import { dataLoopApi } from '@/api'
import type { CandidateResponse, JsonObject, ProposeCandidateRequest } from '@/api'
import NaJsonBlock from '@/components/NaJsonBlock.vue'
import { useAsyncTask } from '@/composables/useAsyncTask'
import { useConfirmStore } from '@/stores/confirm'
import { useToastStore } from '@/stores/toast'
import { describeError } from '@/utils/errors'
import { newIdempotencyKey } from '@/utils/format'
import { firstError, jsonText, parseJson, requiredText } from '@/utils/validate'

const toast = useToastStore()
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
const lookupLoading = ref(false)
const lookupError = ref<string | null>(null)

async function lookup(): Promise<void> {
  const id = lookupId.value.trim()
  if (!id) {
    toast.warn('请填写候选 ID')
    return
  }
  lookupLoading.value = true
  lookupError.value = null
  try {
    lookupResult.value = await dataLoopApi.getCandidate(id)
  } catch (cause) {
    lookupError.value = describeError(cause)
    lookupResult.value = null
  } finally {
    lookupLoading.value = false
  }
}
</script>

<template>
  <div class="na-grid na-grid--detail">
    <section class="na-card">
      <div class="na-card__head">
        <h2>提交配置候选</h2>
        <span class="na-badge na-badge--live">POST /configuration-candidates</span>
      </div>

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
          placeholder="[{&quot;path&quot;: &quot;retrieval.top_k&quot;, &quot;from&quot;: 8, &quot;to&quot;: 12}]"
        />
      </div>

      <div class="na-row na-row--end">
        <button class="na-btn na-btn--primary" type="button" :disabled="pending" @click="submitProposal">
          {{ pending ? '提交中…' : '提交候选' }}
        </button>
      </div>

      <NaJsonBlock v-if="createResult" :value="createResult" label="提交结果" />
    </section>

    <section class="na-card">
      <div class="na-card__head">
        <h2>查询候选详情</h2>
        <span class="na-badge">GET /configuration-candidates/{'{candidate_id}'}</span>
      </div>

      <div class="na-row">
        <div class="na-field na-field--grow">
          <label for="candidate-lookup">候选 ID</label>
          <input
            id="candidate-lookup"
            v-model="lookupId"
            class="na-input"
            autocomplete="off"
            @keyup.enter="lookup"
          >
        </div>
        <div class="na-field na-field--action">
          <button class="na-btn" type="button" :disabled="lookupLoading" @click="lookup">查询</button>
        </div>
      </div>

      <p v-if="lookupError" class="na-message na-message--error">{{ lookupError }}</p>
      <NaJsonBlock
        v-if="lookupResult"
        :value="lookupResult"
        label="候选详情"
        :collapsed-height="380"
      />
      <p v-else-if="!lookupError" class="na-muted">输入候选 ID 后查询其状态与差异。</p>
    </section>
  </div>
</template>
