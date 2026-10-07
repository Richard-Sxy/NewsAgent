<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { dataLoopApi } from '@/api'
import type {
  BootstrapProductionBundleRequest,
  JsonObject,
  ProductionBundleResponse,
  RollbackProductionBundleRequest,
  RollbackProductionBundleResponse,
} from '@/api'
import NaJsonBlock from '@/components/NaJsonBlock.vue'
import NaStateBlock from '@/components/NaStateBlock.vue'
import { useAsyncTask } from '@/composables/useAsyncTask'
import { useConfirmStore } from '@/stores/confirm'
import { useToastStore } from '@/stores/toast'
import { describeError } from '@/utils/errors'
import { newIdempotencyKey } from '@/utils/format'
import { firstError, jsonText, parseJson, requiredText } from '@/utils/validate'

const toast = useToastStore()
const dialog = useConfirmStore()
const { pending, run } = useAsyncTask()

/* ---------------- 当前生效生产包 ---------------- */

const activeBundle = ref<ProductionBundleResponse | null>(null)
const activeLoading = ref(false)
const activeError = ref<string | null>(null)

async function loadActive(): Promise<void> {
  activeLoading.value = true
  activeError.value = null
  try {
    activeBundle.value = await dataLoopApi.activeBundle()
  } catch (cause) {
    activeError.value = describeError(cause)
    activeBundle.value = null
  } finally {
    activeLoading.value = false
  }
}

/* ---------------- 引导生产包 ---------------- */

const bundleVersion = ref('')
const specText = ref('')
const bootstrapResult = ref<ProductionBundleResponse | null>(null)

async function submitBootstrap(): Promise<void> {
  const problem = firstError(
    requiredText(bundleVersion.value, '生产包版本'),
    jsonText(specText.value, '生产包规格'),
  )
  if (problem) {
    toast.warn(problem)
    return
  }

  const body: BootstrapProductionBundleRequest = {
    bundle_version: bundleVersion.value.trim(),
    spec: parseJson<JsonObject>(specText.value, {}),
  }

  const result = await run(() => dataLoopApi.bootstrapBundle(body), {
    success: '生产包引导成功',
    failure: '引导生产包失败',
  })
  if (!result) return
  bootstrapResult.value = result
  await loadActive()
}

/* ---------------- 回滚 ---------------- */

const rollbackTargetId = ref('')
const rollbackReason = ref('')
const rollbackResult = ref<RollbackProductionBundleResponse | null>(null)

async function submitRollback(): Promise<void> {
  const problem = firstError(
    requiredText(rollbackTargetId.value, '目标生产包 ID'),
    requiredText(rollbackReason.value, '回滚原因'),
  )
  if (problem) {
    toast.warn(problem)
    return
  }

  const target = rollbackTargetId.value.trim()
  const confirmed = await dialog.ask({
    title: '确认回滚生产配置？',
    description: `生效生产包将切换到 ${target}。这会直接影响线上生产行为，请确认已完成评审。`,
    confirmText: '确认回滚',
    danger: true,
  })
  if (!confirmed) return

  const body: RollbackProductionBundleRequest = {
    target_bundle_id: target,
    reason: rollbackReason.value.trim(),
    idempotency_key: newIdempotencyKey('bundle-rollback'),
  }

  const result = await run(() => dataLoopApi.rollbackBundle(body), {
    success: '回滚已提交',
    failure: '回滚生产包失败',
  })
  if (!result) return
  rollbackResult.value = result
  rollbackReason.value = ''
  await loadActive()
}

onMounted(loadActive)
</script>

<template>
  <section class="na-card">
    <div class="na-card__head">
      <h2>当前生效生产包</h2>
      <div class="na-row">
        <span class="na-badge">GET /production-bundles/active</span>
        <button class="na-btn" type="button" :disabled="activeLoading" @click="loadActive">
          {{ activeLoading ? '加载中…' : '刷新' }}
        </button>
      </div>
    </div>

    <NaStateBlock
      :loading="activeLoading && !activeBundle"
      :error="activeError"
      :empty="!activeBundle"
      empty-text="后端未返回生效中的生产包"
      loading-text="查询生效生产包…"
    >
      <NaJsonBlock v-if="activeBundle" :value="activeBundle" label="生效生产包" />
    </NaStateBlock>
  </section>

  <div class="na-grid na-grid--split">
    <section class="na-card">
      <div class="na-card__head">
        <h2>引导生产包</h2>
        <span class="na-badge">POST /production-bundles/bootstrap</span>
      </div>

      <div class="na-field">
        <label for="bundle-version">生产包版本</label>
        <input id="bundle-version" v-model="bundleVersion" class="na-input" autocomplete="off">
      </div>

      <div class="na-field">
        <label for="bundle-spec">生产包规格（JSON 对象）</label>
        <textarea
          id="bundle-spec"
          v-model="specText"
          class="na-textarea"
          placeholder="粘贴 ProductionBundleSpec"
        />
      </div>

      <div class="na-row na-row--end">
        <button class="na-btn na-btn--primary" type="button" :disabled="pending" @click="submitBootstrap">
          {{ pending ? '提交中…' : '引导生产包' }}
        </button>
      </div>

      <NaJsonBlock v-if="bootstrapResult" :value="bootstrapResult" label="引导结果" />
    </section>

    <section class="na-card">
      <div class="na-card__head">
        <h2>回滚生产包</h2>
        <span class="na-badge na-badge--danger">POST /production-bundles/rollback</span>
      </div>

      <p class="na-muted">回滚是直接影响线上的动作，提交前会弹二次确认。</p>

      <div class="na-field">
        <label for="rollback-target">目标生产包 ID</label>
        <input id="rollback-target" v-model="rollbackTargetId" class="na-input" autocomplete="off">
      </div>

      <div class="na-field">
        <label for="rollback-reason">回滚原因</label>
        <input id="rollback-reason" v-model="rollbackReason" class="na-input">
      </div>

      <div class="na-row na-row--end">
        <button class="na-btn na-btn--danger" type="button" :disabled="pending" @click="submitRollback">
          {{ pending ? '提交中…' : '执行回滚' }}
        </button>
      </div>

      <NaJsonBlock v-if="rollbackResult" :value="rollbackResult" label="回滚结果" />
    </section>
  </div>
</template>
