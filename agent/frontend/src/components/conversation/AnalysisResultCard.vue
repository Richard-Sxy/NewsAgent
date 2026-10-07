<script setup lang="ts">
import { computed } from 'vue'
import type { ConversationToolTrace } from '@/api/conversations'
import { projectAnalysisResult } from '@/utils/analysisResult'

const props = defineProps<{ trace: ConversationToolTrace }>()
const result = computed(() => projectAnalysisResult(props.trace))
</script>

<template>
  <section v-if="result" class="na-analysis-result" aria-label="确定性数据分析结果">
    <h3>{{ result.title }} <small>{{ result.metric }}</small></h3>
    <p class="na-muted">仅覆盖已保存热点榜单 · 当前 {{ result.source.rowCount }} 条新闻</p>
    <p class="na-muted">当前窗口：{{ result.source.windowStart }} 至 {{ result.source.windowEnd }}</p>
    <p v-if="result.reference" class="na-muted">
      参考窗口：{{ result.reference.windowStart }} 至 {{ result.reference.windowEnd }}
      · {{ result.reference.rowCount }} 条新闻
    </p>
    <p v-if="result.unsafeInteger" class="na-notice">
      部分整数超出浏览器精确范围，卡片未展示该数值；请以 Python 已保存的权威答复为准。
    </p>
    <dl class="na-analysis-result__fields">
      <div v-for="field in result.fields" :key="field.label">
        <dt>{{ field.label }}</dt><dd>{{ field.value }}</dd>
      </div>
    </dl>
    <div
      v-if="result.columns.length"
      class="na-analysis-result__table"
      tabindex="0"
      role="region"
      aria-label="分析对照表，可横向滚动"
    >
      <table :style="{ '--analysis-columns': result.columns.length }">
        <thead><tr><th v-for="column in result.columns" :key="column">{{ column }}</th></tr></thead>
        <tbody>
          <tr v-for="(row, index) in result.rows" :key="index">
            <td v-for="(value, column) in row" :key="column">{{ value }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <ul v-if="result.notes.length" class="na-muted">
      <li v-for="note in result.notes" :key="note">{{ note }}</li>
    </ul>
    <ul v-if="result.limitations.length" class="na-muted">
      <li v-for="limitation in result.limitations" :key="limitation">{{ limitation }}</li>
    </ul>
    <details class="na-analysis-result__evidence">
      <summary>计算证据与资源审计</summary>
      <section v-for="(source, index) in [result.source, result.reference].filter(Boolean)" :key="index">
        <h4>{{ index === 0 ? '当前来源' : '参考来源' }}</h4>
        <dl v-if="source" class="na-analysis-result__fields">
          <div><dt>运行编号</dt><dd>{{ source.runId }}</dd></div>
          <div><dt>规则包版本</dt><dd>{{ source.bundle }}</dd></div>
          <div><dt>范围</dt><dd>已保存排行（ranked_news_only）</dd></div>
          <div><dt>快照 SHA-256</dt><dd class="na-mono">{{ source.hash }}</dd></div>
          <div v-for="field in source.provenance" :key="field.label">
            <dt>{{ field.label }}</dt><dd>{{ field.value }}</dd>
          </div>
        </dl>
      </section>
      <h4>本次实际执行</h4>
      <dl class="na-analysis-result__fields">
        <div v-for="field in result.execution" :key="field.label">
          <dt>{{ field.label }}</dt><dd>{{ field.value }}</dd>
        </div>
      </dl>
    </details>
  </section>
</template>

<style scoped>
.na-analysis-result { margin-top: 16px; padding: 16px; border: 1px solid var(--na-border, #ddd); border-radius: 10px; background: var(--na-bg, #fafafa); }
h3 { margin: 0 0 8px; }
h3 small { margin-left: 8px; font-size: 13px; font-weight: normal; }
.na-analysis-result__fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; }
dt { color: var(--na-text-muted, #667085); font-size: 12px; }
dd { margin: 4px 0 0; overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
.na-analysis-result__table { overflow-x: auto; }
.na-analysis-result__table:focus-visible { outline: 2px solid var(--na-accent); outline-offset: 2px; }
table { border-collapse: collapse; table-layout: fixed; width: calc(var(--analysis-columns) * 8.5rem); min-width: 100%; font-size: 13px; }
th, td { width: 8.5rem; padding: 8px; text-align: left; white-space: normal; overflow-wrap: anywhere; vertical-align: top; border-bottom: 1px solid var(--na-border, #ddd); }
td { font-variant-numeric: tabular-nums; }
.na-analysis-result__evidence { margin-top: 12px; }
summary { cursor: pointer; }
</style>
