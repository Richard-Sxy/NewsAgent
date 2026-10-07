<script setup lang="ts">
import type { HotNewsSqlToolTraceView } from '@/api'
import type { SqlAssistantStage } from '@/api/sqlAssistant'
import NaJsonBlock from '@/components/NaJsonBlock.vue'
import { formatMetricCell } from '@/utils/format'

defineProps<{ runId: string; trace: HotNewsSqlToolTraceView }>()

function stageLabel(name: string): string {
  const labels: Record<string, string> = {
    input_boundary: '输入与权限',
    model_planning: '受限查询计划生成',
    sql_guard: 'SQL 安全校验',
    preview_saved: '查询计划保存',
    snapshot_recheck: '执行前快照复核',
    warehouse_execute: '本地聚合数据查询',
    result_saved: 'SQL 结果保存',
    metric_mapping: '指标快照转换',
    hot_news_handoff: '交给热点 Agent',
    metric_source: '热点取数工具',
    supplemental_query: '补充指标与基线查询',
  }
  return labels[name] ?? name
}

function stageClass(stage: SqlAssistantStage): string {
  return `na-badge na-badge--${stage.status === 'passed' ? 'live' : stage.status === 'degraded' ? 'sample' : 'danger'}`
}

function statusLabel(status: SqlAssistantStage['status']): string {
  return { passed: '通过', blocked: '阻断', degraded: '降级' }[status]
}

function columnLabel(column: string): string {
  const labels: Record<string, string> = {
    news_id: '新闻 ID',
    title: '新闻标题',
    content_type: '内容类型',
    category: '分类',
    source: '来源',
    event_time: '聚合时间',
    impressions: '曝光量',
    clicks: '点击量',
    effective_consumptions: '有效消费量',
    interactions: '互动量',
    ctr: '点击率',
    hot_score: 'SQL 筛选参考分',
  }
  return labels[column] ?? column
}

function cellText(value: string | number | null | undefined, column: string): string {
  if (value === null || value === undefined) return '—'
  if (column === 'content_type')
    return { article: '图文', video: '视频' }[String(value)] ?? String(value)
  return formatMetricCell(value, column)
}
</script>

<template>
  <section class="na-card sql-tool-trace">
    <div class="na-card__head">
      <div>
        <h2>本次热点运行 · SQL 工具调用</h2>
        <p class="na-muted">{{ trace.preview.question }}</p>
      </div>
      <span class="na-badge na-badge--live">{{ trace.result.row_count }} 个候选 · {{ trace.result.elapsed_ms }} ms</span>
    </div>
    <dl class="na-defs">
      <div>
        <dt>热点运行 ID</dt>
        <dd class="na-mono">{{ runId }}</dd>
      </div>
      <div>
        <dt>SQL 工具查询 ID</dt>
        <dd class="na-mono">{{ trace.query_id }}</dd>
      </div>
      <div>
        <dt>场景 / 模型</dt>
        <dd class="na-mono">
          {{ trace.preview.scenario_id }} / {{ trace.preview.model_provider }}
        </dd>
      </div>
    </dl>
    <div class="na-notice sql-tool-trace__notice">
      SQL 排序只决定候选筛选顺序。下面的 SQL 参考分不等于最终热度；热点 Agent 使用可信指标与基线，由
      Python HotNewsRanker 再计算并排序。
    </div>
    <div class="sql-tool-trace__grid">
      <div>
        <h3>实际 SQL</h3>
        <pre class="na-log sql-tool-trace__code">{{ trace.preview.sql }}</pre>
      </div>
      <div>
        <NaJsonBlock
          :value="trace.preview.parameters"
          label="身份与窗口绑定参数"
          :collapsed-height="260"
        />
        <p class="na-muted sql-tool-trace__note">{{ trace.preview.explanation }}</p>
      </div>
    </div>
    <details class="sql-tool-trace__details">
      <summary>查看候选筛选输入（与本次热点运行绑定）</summary>
      <p class="na-muted">{{ trace.result.summary }}</p>
      <div class="sql-tool-trace__table">
        <table class="na-table">
          <thead>
            <tr>
              <th v-for="column in trace.result.columns" :key="column">
                {{ columnLabel(column) }}
                <div class="na-cell__sub">{{ column }}</div>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, index) in trace.result.rows" :key="index">
              <td v-for="column in trace.result.columns" :key="column" :title="row[column] == null ? undefined : String(row[column])">
                {{ cellText(row[column], column) }}
              </td>
            </tr>
            <tr v-if="!trace.result.rows.length">
              <td :colspan="Math.max(trace.result.columns.length, 1)" class="na-empty">
                当前窗口没有匹配的候选新闻。
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </details>
    <div v-if="trace.metric_mapping_notes?.length" class="sql-tool-trace__notes">
      <h3>SQL 结果如何进入热点 Agent</h3>
      <p v-for="note in trace.metric_mapping_notes" :key="note" class="na-muted">{{ note }}</p>
    </div>
    <details v-if="trace.supplemental_sql" class="sql-tool-trace__details">
      <summary>查看补充指标与基线查询</summary>
      <pre class="na-log sql-tool-trace__code">{{ trace.supplemental_sql }}</pre>
    </details>
    <h3 class="sql-tool-trace__heading">SQL 工具阶段记录</h3>
    <div class="sql-tool-trace__table">
      <table class="na-table">
        <thead>
          <tr>
            <th>阶段</th>
            <th>状态</th>
            <th>说明</th>
            <th>耗时</th>
            <th>尝试</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(stage, index) in trace.result.stages" :key="`${index}-${stage.name}`">
            <td class="sql-tool-trace__nowrap">{{ index + 1 }}. {{ stageLabel(stage.name) }}</td>
            <td>
              <span :class="stageClass(stage)">{{ statusLabel(stage.status) }}</span>
            </td>
            <td>{{ stage.detail }}</td>
            <td class="sql-tool-trace__nowrap">{{ stage.elapsed_ms }} ms</td>
            <td>{{ stage.attempts }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <details class="sql-tool-trace__details">
      <summary>查看版本与完整性标识</summary>
      <dl class="na-defs sql-tool-trace__metadata">
        <div>
          <dt>Schema 版本 / SHA-256</dt>
          <dd class="na-mono">
            {{ trace.preview.schema_version }} / {{ trace.preview.schema_sha256 }}
          </dd>
        </div>
        <div>
          <dt>SQL SHA-256</dt>
          <dd class="na-mono">{{ trace.preview.sql_hash }}</dd>
        </div>
        <div>
          <dt>模型请求 ID</dt>
          <dd class="na-mono">{{ trace.preview.model_request_id ?? '确定性模板' }}</dd>
        </div>
        <div v-if="trace.scope_sha256">
          <dt>查询作用域 SHA-256</dt>
          <dd class="na-mono">{{ trace.scope_sha256 }}</dd>
        </div>
      </dl>
    </details>
  </section>
</template>

<style scoped>
.sql-tool-trace__notice,
.sql-tool-trace__metadata {
  margin-top: 12px;
}
.sql-tool-trace__grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 16px;
  margin-top: 14px;
}
@media (min-width: 1100px) {
  .sql-tool-trace__grid {
    grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);
  }
}
.sql-tool-trace__code {
  margin-top: 8px;
  max-height: 440px;
  white-space: pre-wrap;
  word-break: break-word;
}
.sql-tool-trace__note {
  font-size: 12px;
}
.sql-tool-trace__details {
  margin-top: 12px;
}
.sql-tool-trace__details summary {
  cursor: pointer;
  color: var(--na-accent);
}
.sql-tool-trace__table {
  overflow-x: auto;
}
.sql-tool-trace__table td {
  cursor: default;
}
.sql-tool-trace__nowrap {
  white-space: nowrap;
}
.sql-tool-trace__notes,
.sql-tool-trace__heading {
  margin-top: 16px;
}
.sql-tool-trace__notes p {
  margin: 6px 0;
}
</style>
