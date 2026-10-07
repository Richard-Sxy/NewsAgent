<script setup lang="ts">
import type { WritingJob } from '@/api'
import { formatDateTime, formatPercent } from '@/utils/format'
import { jobStatusLabel, jobStatusTone } from '@/utils/labels'

defineProps<{
  items: WritingJob[]
  selectedId: string | null
}>()

defineEmits<{ (event: 'select', jobId: string): void }>()
</script>

<template>
  <table class="na-table">
    <thead>
      <tr>
        <th>选题</th>
        <th style="width: 104px">状态</th>
        <th style="width: 76px">进度</th>
        <th style="width: 138px">更新时间</th>
      </tr>
    </thead>
    <tbody>
      <tr
        v-for="job in items"
        :key="job.id"
        :class="{ 'is-selected': job.id === selectedId }"
        @click="$emit('select', job.id)"
      >
        <td>
          <div>{{ job.topic }}</div>
          <div class="na-cell__sub">{{ job.id }}</div>
        </td>
        <td>
          <span class="na-badge" :class="`na-badge--${jobStatusTone(job.status)}`">
            {{ jobStatusLabel(job.status) }}
          </span>
        </td>
        <td>{{ formatPercent(job.progress_percent) }}</td>
        <td class="na-muted">{{ formatDateTime(job.updated_at) }}</td>
      </tr>
    </tbody>
  </table>
</template>
