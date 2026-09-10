<script setup lang="ts">
import { ref } from 'vue'

import { jobsApi } from '@/api'
import type { CreateWritingJobRequest, JobScenario } from '@/api'
import { useAsyncTask } from '@/composables/useAsyncTask'
import { useToastStore } from '@/stores/toast'
import { newIdempotencyKey } from '@/utils/format'
import { firstError, jsonText, parseJson, requiredText } from '@/utils/validate'

const emit = defineEmits<{ (event: 'created', jobId: string): void }>()

const toast = useToastStore()
const { pending, run } = useAsyncTask()

const topic = ref('')
const scenario = ref<JobScenario>('assisted_writing')
const showRequirements = ref(false)
const requirementsText = ref('')

async function submit(): Promise<void> {
  const problem = firstError(
    requiredText(topic.value, '选题'),
    showRequirements.value
      ? jsonText(requirementsText.value, '补充要求', { allowEmpty: true })
      : null,
  )
  if (problem) {
    toast.warn(problem)
    return
  }

  const trimmedRequirements = requirementsText.value.trim()
  const body: CreateWritingJobRequest = {
    topic: topic.value.trim(),
    scenario: scenario.value,
    idempotency_key: newIdempotencyKey('job'),
    ...(showRequirements.value && trimmedRequirements
      ? { requirements: parseJson<Record<string, unknown>>(trimmedRequirements, {}) }
      : {}),
  }

  const job = await run(() => jobsApi.create(body), {
    // 幂等键随请求一起生成，重复点击不会产生两个任务。
    success: `已创建任务：${body.topic}`,
    failure: '创建任务失败',
  })
  if (!job) return

  topic.value = ''
  requirementsText.value = ''
  emit('created', job.id)
}
</script>

<template>
  <section class="na-card">
    <div class="na-card__head">
      <h2>新建写作任务</h2>
      <span class="na-badge na-badge--live">真实接口</span>
    </div>

    <form class="na-row" @submit.prevent="submit">
      <div class="na-field na-field--grow">
        <label for="job-topic">选题</label>
        <input
          id="job-topic"
          v-model="topic"
          class="na-input"
          placeholder="例如：某行业季度数据发布"
          autocomplete="off"
        >
      </div>
      <div class="na-field na-field--fixed">
        <label for="job-scenario">产出类型</label>
        <select id="job-scenario" v-model="scenario" class="na-select">
          <option value="assisted_writing">辅助写作（含成稿）</option>
          <option value="research_package">仅资料包</option>
        </select>
      </div>
      <div class="na-field na-field--action">
        <button
          class="na-btn"
          type="button"
          :aria-expanded="showRequirements"
          @click="showRequirements = !showRequirements"
        >
          {{ showRequirements ? '收起补充要求' : '补充要求' }}
        </button>
      </div>
      <div class="na-field na-field--action">
        <button class="na-btn na-btn--primary" type="submit" :disabled="pending">
          {{ pending ? '提交中…' : '创建任务' }}
        </button>
      </div>
    </form>

    <div v-if="showRequirements" class="na-field">
      <label for="job-requirements">补充要求（JSON 对象，可留空）</label>
      <textarea
        id="job-requirements"
        v-model="requirementsText"
        class="na-textarea"
        placeholder="例如：{&quot;tone&quot;: &quot;客观&quot;, &quot;length&quot;: 1800}"
      />
    </div>
  </section>
</template>
