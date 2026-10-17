<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

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

const activeVersion = computed(() => {
  const version = activeBundle.value?.bundle?.bundle_version
  return typeof version === 'string' && version.trim() ? version : '未知'
})
const activeStatus = computed(() => {
  const status = activeBundle.value?.bundle?.status
  return status === 'active' ? '生效中' : status === 'inactive' ? '已停用' : '未知'
})

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
      <button class="na-btn na-btn--quiet" type="button" :disabled="activeLoading" @click="loadActive">
        {{ activeLoading ? '加载中…' : '刷新' }}
      </button>
    </div>

    <NaStateBlock
      :loading="activeLoading && !activeBundle"
      :error="activeError"
      :empty="!activeBundle"
      empty-text="后端未返回生效中的生产包"
      loading-text="查询生效生产包…"
    >
      <template v-if="activeBundle">
        <dl class="bundle-summary">
          <div>
            <dt>当前版本</dt>
            <dd class="bundle-summary__version">{{ activeVersion }}</dd>
          </div>
          <div>
            <dt>状态</dt>
            <dd>
              <span class="na-badge" :class="activeBundle.bundle?.status === 'active' ? 'na-badge--live' : 'na-badge--neutral'">
                {{ activeStatus }}
              </span>
            </dd>
          </div>
        </dl>
        <details class="na-disclosure">
          <summary>完整配置与版本信息</summary>
          <NaJsonBlock :value="activeBundle" label="生效生产包" />
        </details>
      </template>
    </NaStateBlock>
  </section>

  <details class="na-card na-disclosure bundles-advanced">
    <summary>
      高级操作
      <span class="bundles-advanced__hint">初始化与回滚生产配置</span>
    </summary>

    <div class="bundle-actions">
      <section class="bundle-action" aria-labelledby="bundle-bootstrap-heading">
        <h3 id="bundle-bootstrap-heading">引导生产包</h3>

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

        <details v-if="bootstrapResult" class="na-disclosure">
          <summary>查看引导结果</summary>
          <NaJsonBlock :value="bootstrapResult" label="引导结果" />
        </details>
      </section>

      <section class="bundle-action" aria-labelledby="bundle-rollback-heading">
        <h3 id="bundle-rollback-heading">回滚生产包</h3>

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

        <details v-if="rollbackResult" class="na-disclosure">
          <summary>查看回滚结果</summary>
          <NaJsonBlock :value="rollbackResult" label="回滚结果" />
        </details>
      </section>
    </div>
  </details>
</template>

<style scoped>
.bundle-summary {
  display: flex;
  align-items: start;
  justify-content: space-between;
  gap: 24px;
  margin: 24px 0;
}

.bundle-summary > div {
  min-width: 0;
}

.bundle-summary dt {
  margin-bottom: 8px;
  color: var(--na-text-muted);
  font-size: 12px;
}

.bundle-summary dd {
  margin: 0;
}

.bundle-summary__version {
  font-size: 22px;
  font-weight: 600;
  overflow-wrap: anywhere;
}

.bundle-summary > div:last-child {
  flex-shrink: 0;
  text-align: right;
}

.bundles-advanced {
  margin-top: 0;
  padding-block: 0;
}

.bundles-advanced > summary {
  padding-block: 16px;
  color: var(--na-text);
  font-weight: 500;
}

.bundles-advanced__hint {
  margin-left: 12px;
  color: var(--na-text-muted);
  font-size: 12px;
  font-weight: 400;
}

.bundle-actions {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 24px;
  margin-bottom: 20px;
}

.bundle-action {
  min-width: 0;
  padding-top: 12px;
}

.bundle-action + .bundle-action {
  padding-left: 24px;
  border-left: 1px solid var(--na-border);
}

.bundle-action h3 {
  margin-bottom: 16px;
}

@media (max-width: 850px) {
  .bundle-actions {
    grid-template-columns: minmax(0, 1fr);
  }

  .bundle-action + .bundle-action {
    padding-top: 24px;
    padding-left: 0;
    border-top: 1px solid var(--na-border);
    border-left: none;
  }
}

@media (max-width: 600px) {
  .bundles-advanced__hint {
    display: block;
    margin-left: 18px;
  }
}
</style>
