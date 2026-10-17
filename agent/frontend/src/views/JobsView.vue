<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import type { JobStatus } from '@/api'
import JobCreateCard from '@/components/jobs/JobCreateCard.vue'
import JobInspector from '@/components/jobs/JobInspector.vue'
import JobTable from '@/components/jobs/JobTable.vue'
import NaPaginator from '@/components/NaPaginator.vue'
import NaStateBlock from '@/components/NaStateBlock.vue'
import { useJobsStore } from '@/stores/jobs'
import { writingJobId } from '@/utils/capabilityReceipts'

const store = useJobsStore()
const route = useRoute()
const router = useRouter()

const STATUS_FILTERS: Array<{ value: JobStatus | ''; label: string }> = [
  { value: '', label: '全部状态' },
  { value: 'waiting_human', label: '待人工处理' },
  { value: 'drafting', label: '撰写中' },
  { value: 'reviewing', label: '评审中' },
  { value: 'final_approved', label: '终审通过' },
  { value: 'published', label: '已发布' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
]

const PAGE_SIZES = [10, 20, 50]

const waitingOnly = ref(true)
const statusFilter = ref<JobStatus | ''>('')
const keyword = ref('')
const pageSize = ref(20)
const offset = ref(0)

async function reload(): Promise<void> {
  await store.fetchList({
    status: statusFilter.value ? [statusFilter.value] : undefined,
    waiting_human_only: waitingOnly.value || undefined,
    query: keyword.value.trim() || undefined,
    limit: pageSize.value,
    offset: offset.value,
  })
}

function applyFilters(): void {
  offset.value = 0
  void reload()
}

function changePage(next: number): void {
  offset.value = next
  void reload()
}

function changePageSize(): void {
  offset.value = 0
  void reload()
}

function handleSelect(jobId: string): void {
  void router.replace({ query: { ...route.query, job_id: jobId } })
}

async function handleCreated(jobId: string): Promise<void> {
  offset.value = 0
  await reload()
  handleSelect(jobId)
}

// The linked job is loaded independently of list filters and pagination.
watch(() => route.query.job_id, (value) => {
  const jobId = writingJobId(value)
  if (jobId && store.selectedId !== jobId) store.openJob(jobId)
  else if (!jobId) store.select(null)
}, { immediate: true })

onMounted(reload)
onBeforeUnmount(() => store.disconnectStream())
</script>

<template>
  <header class="na-page-head">
    <div>
      <p class="na-eyebrow">内容工作台</p>
      <h1>写作任务</h1>
      <p class="na-subtitle">处理人工待办，查看写作进度与成稿。</p>
    </div>
    <button class="na-btn na-btn--quiet" type="button" :disabled="store.loading" @click="reload">
      {{ store.loading ? '加载中…' : '刷新任务' }}
    </button>
  </header>

  <details class="na-card na-disclosure jobs-create">
    <summary>
      <span class="jobs-create__title">新建写作任务</span>
      <span class="jobs-create__hint">填写选题，开始研究与写作</span>
      <span class="jobs-create__action" aria-hidden="true">＋</span>
    </summary>
    <JobCreateCard @created="handleCreated" />
  </details>

  <div class="na-grid na-grid--detail jobs-workspace">
    <section class="na-card jobs-list">
      <div class="na-card__head">
        <h2>{{ waitingOnly ? '待人工处理' : '任务列表' }}</h2>
        <span class="na-badge">{{ store.total }} 个任务</span>
      </div>

      <div class="jobs-filters">
        <div class="na-field na-field--grow">
          <label for="job-keyword">关键词</label>
          <input
            id="job-keyword"
            v-model="keyword"
            class="na-input"
            placeholder="按选题搜索，回车触发"
            autocomplete="off"
            @keyup.enter="applyFilters"
          >
        </div>
        <div class="na-field na-field--fixed">
          <label for="job-status">状态</label>
          <select id="job-status" v-model="statusFilter" class="na-select" @change="applyFilters">
            <option v-for="item in STATUS_FILTERS" :key="item.value" :value="item.value">
              {{ item.label }}
            </option>
          </select>
        </div>
        <div class="na-field na-field--check">
          <label for="job-waiting">
            <input id="job-waiting" v-model="waitingOnly" type="checkbox" @change="applyFilters">
            仅待人工处理
          </label>
        </div>
        <div class="na-field na-field--action">
          <button class="na-btn na-btn--primary" type="button" @click="applyFilters">搜索</button>
        </div>
      </div>

      <NaStateBlock
        :loading="store.loading && store.items.length === 0"
        :error="store.listError"
        :empty="store.items.length === 0"
        empty-text="当前筛选条件下没有任务"
        loading-text="加载任务列表…"
      >
        <div class="na-table-wrap jobs-table" tabindex="0" aria-label="写作任务列表，可横向滚动">
          <JobTable :items="store.items" :selected-id="store.selectedId" @select="handleSelect" />
        </div>
      </NaStateBlock>

      <details class="na-disclosure jobs-settings">
        <summary>列表设置</summary>
        <div class="na-field na-field--fixed">
          <label for="job-page-size">每页</label>
          <select id="job-page-size" v-model.number="pageSize" class="na-select" @change="changePageSize">
            <option v-for="size in PAGE_SIZES" :key="size" :value="size">{{ size }} 条</option>
          </select>
        </div>
      </details>

      <NaPaginator
        :total="store.total"
        :limit="pageSize"
        :offset="offset"
        @change="changePage"
      />
    </section>

    <JobInspector :job-id="store.selectedId" />
  </div>
</template>

<style scoped>
.jobs-create {
  margin-top: 0;
  padding: 0 20px;
}

.jobs-create > summary {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px 0;
  list-style: none;
}

.jobs-create > summary::-webkit-details-marker {
  display: none;
}

.jobs-create__title {
  color: var(--na-text);
  font-weight: 600;
}

.jobs-create__hint {
  flex: 1;
  color: var(--na-text-muted);
  font-size: 12px;
}

.jobs-create__action {
  margin-left: auto;
  color: var(--na-accent);
  font-size: 20px;
  line-height: 1;
  transition: transform 150ms ease;
}

.jobs-create[open] .jobs-create__action {
  transform: rotate(45deg);
}

.jobs-create :deep(.na-card) {
  margin-bottom: 20px;
  padding: 0;
  border: none;
  box-shadow: none;
}

.jobs-workspace {
  align-items: start;
  grid-template-columns: minmax(0, 1fr);
}

@media (min-width: 1280px) {
  .jobs-workspace { grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); }
}

.jobs-workspace > * {
  min-width: 0;
}

.jobs-filters {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(130px, 160px);
  gap: 0 12px;
  align-items: end;
  margin-bottom: 14px;
}

.jobs-filters .na-field--action {
  align-items: flex-end;
}

.jobs-filters .na-field--check {
  min-height: 37px;
}

.jobs-table :deep(.na-table) {
  min-width: 580px;
}

.jobs-settings {
  border-top: none;
}

.jobs-settings .na-field {
  max-width: 180px;
}

@media (max-width: 600px) {
  .jobs-create {
    padding-inline: 16px;
  }

  .jobs-create__hint {
    display: none;
  }

  .jobs-filters {
    grid-template-columns: minmax(0, 1fr);
  }

  .jobs-filters .na-field--action {
    align-items: stretch;
  }
}
</style>
