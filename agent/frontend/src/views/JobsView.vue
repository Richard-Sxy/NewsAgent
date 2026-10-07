<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'

import type { JobStatus } from '@/api'
import JobCreateCard from '@/components/jobs/JobCreateCard.vue'
import JobInspector from '@/components/jobs/JobInspector.vue'
import JobTable from '@/components/jobs/JobTable.vue'
import NaPaginator from '@/components/NaPaginator.vue'
import NaStateBlock from '@/components/NaStateBlock.vue'
import { useJobsStore } from '@/stores/jobs'

const store = useJobsStore()

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
  store.openJob(jobId)
}

async function handleCreated(jobId: string): Promise<void> {
  offset.value = 0
  await reload()
  store.openJob(jobId)
}

onMounted(reload)
onBeforeUnmount(() => store.disconnectStream())
</script>

<template>
  <JobCreateCard @created="handleCreated" />

  <div class="na-grid na-grid--detail">
    <section class="na-card">
      <div class="na-card__head">
        <h2>任务列表</h2>
        <button class="na-btn" type="button" :disabled="store.loading" @click="reload">
          {{ store.loading ? '加载中…' : '刷新' }}
        </button>
      </div>

      <div class="na-row">
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
        <div class="na-field na-field--fixed">
          <label for="job-page-size">每页</label>
          <select id="job-page-size" v-model.number="pageSize" class="na-select" @change="changePageSize">
            <option v-for="size in PAGE_SIZES" :key="size" :value="size">{{ size }} 条</option>
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
        <JobTable :items="store.items" :selected-id="store.selectedId" @select="handleSelect" />
      </NaStateBlock>

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
