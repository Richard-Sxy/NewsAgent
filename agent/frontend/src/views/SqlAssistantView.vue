<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { sqlAssistantApi } from '@/api/sqlAssistant'
import type {
  SqlAssistantConfig,
  SqlAssistantPreview,
  SqlAssistantResult,
  SqlAssistantStage,
} from '@/api/sqlAssistant'
import NaJsonBlock from '@/components/NaJsonBlock.vue'
import { useAsyncTask } from '@/composables/useAsyncTask'
import { downloadBlob } from '@/utils/download'
import { formatMetricCell } from '@/utils/format'

const configuration = ref<SqlAssistantConfig | null>(null)
const question = ref('')
const scenarioId = ref('')
const windowStart = ref('')
const windowEnd = ref('')
const configurationTemplate = ref('')
const preview = ref<SqlAssistantPreview | null>(null)
const result = ref<SqlAssistantResult | null>(null)
const action = ref<'config' | 'preview' | 'execute' | null>(null)
const validationError = ref('')
const { pending, error, run } = useAsyncTask()

const selectedScenario = computed(() =>
  configuration.value?.scenarios.find((scenario) => scenario.id === scenarioId.value),
)
const stages = computed(() => result.value?.stages ?? preview.value?.stages ?? [])
const canPreview = computed(() =>
  Boolean(
    configuration.value &&
    question.value.trim() &&
    scenarioId.value &&
    windowStart.value &&
    windowEnd.value,
  ),
)

// 修改查询范围后，之前的 SQL 快照与结果立即失效，避免执行已过期的预览。
watch(
  [question, scenarioId, windowStart, windowEnd],
  () => {
    preview.value = null
    result.value = null
    validationError.value = ''
    error.value = null
  },
  { flush: 'sync' },
)

function localDateInput(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (number: number) => String(number).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function displayDate(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

async function loadConfiguration(): Promise<void> {
  if (pending.value) return
  action.value = 'config'
  const value = await run(() => sqlAssistantApi.config(), { failure: '无法读取本地查询配置' })
  action.value = null
  if (!value) return
  configuration.value = value
  scenarioId.value = value.scenarios[0]?.id ?? ''
  question.value = value.scenarios[0]?.sample_questions[0] ?? ''
  windowStart.value = localDateInput(value.dataset.window_start)
  windowEnd.value = localDateInput(value.dataset.window_end)
  configurationTemplate.value = value.configuration_template
}

function changeScenario(): void {
  question.value = selectedScenario.value?.sample_questions[0] ?? ''
}

function restoreDatasetWindow(): void {
  if (!configuration.value) return
  windowStart.value = localDateInput(configuration.value.dataset.window_start)
  windowEnd.value = localDateInput(configuration.value.dataset.window_end)
}

async function generatePreview(): Promise<void> {
  if (pending.value || !canPreview.value) return
  const start = new Date(windowStart.value)
  const end = new Date(windowEnd.value)
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime()) || start >= end) {
    validationError.value = '请输入有效时间窗口，结束时间必须晚于开始时间。'
    return
  }
  preview.value = null
  result.value = null
  validationError.value = ''
  action.value = 'preview'
  const value = await run(
    () =>
      sqlAssistantApi.preview({
        question: question.value.trim(),
        scenario_id: scenarioId.value,
        window_start: start.toISOString(),
        window_end: end.toISOString(),
      }),
    { failure: 'SQL 预览生成失败' },
  )
  action.value = null
  if (value) preview.value = value
}

async function executeQuery(): Promise<void> {
  if (pending.value || !preview.value) return
  const queryId = preview.value.query_id
  result.value = null
  action.value = 'execute'
  const value = await run(() => sqlAssistantApi.execute(queryId), { failure: '只读查询执行失败' })
  action.value = null
  if (value) result.value = value
}

function stageLabel(name: string): string {
  const labels: Record<string, string> = {
    input_boundary: '输入与权限',
    prompt_screening: '提示词检查',
    schema_contract: '冻结数据库契约',
    scenario_binding: '场景与版本绑定',
    intent_parse: '查询意图解析',
    intent_resolution: '查询意图解析',
    model_generation: 'Text2SQL 生成',
    model_planning: '受限查询计划生成',
    sql_generation: 'Text2SQL 生成',
    sql_ast_guard: 'SQL 安全校验',
    sql_guard: 'SQL 安全校验',
    preview_snapshot: 'SQL 快照保存',
    preview_saved: 'SQL 快照保存',
    execution_gate: '执行前校验',
    snapshot_recheck: '执行前快照复核',
    database_query: '本地数据库执行',
    sql_execution: '本地数据库执行',
    warehouse_execute: '本地数据库执行',
    result_validation: '查询结果校验',
    result_summary: '确定性结果摘要',
    result_saved: '查询结果保存',
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
    bucket_start: '聚合时间',
    impressions: '曝光量',
    clicks: '点击量',
    effective_consumptions: '有效消费量',
    interactions: '互动量',
    shares: '分享量',
    comments: '评论量',
    likes: '点赞量',
    favorites: '收藏量',
    ctr: '点击率',
    click_through_rate: '点击率',
    avg_dwell_seconds: '平均停留（秒）',
    dwell_seconds: '总停留（秒）',
    hot_score: '热度分',
    news_count: '新闻数',
    total_clicks: '总点击量',
    total_impressions: '总曝光量',
    rank: '排名',
  }
  return labels[column] ?? column
}

function cellText(value: string | number | null | undefined, column: string): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string' && column === 'event_time') return displayDate(value)
  return formatMetricCell(value, column)
}

function downloadSchema(): void {
  if (!configuration.value) return
  downloadBlob(
    new Blob([configuration.value.schema_markdown], { type: 'text/markdown;charset=utf-8' }),
    `news-warehouse-schema-${configuration.value.schema_version}.md`,
  )
}

function downloadTemplate(): void {
  downloadBlob(
    new Blob([configurationTemplate.value], { type: 'text/yaml;charset=utf-8' }),
    'text2sql-scenes.yml',
  )
}

onMounted(loadConfiguration)
</script>

<template>
  <section class="na-card sql-intro">
    <div class="na-card__head">
      <div>
        <h2>SQL 查询助手</h2>
        <p class="na-muted">用自然语言查询新闻聚合指标，先查看 SQL，再执行只读查询。</p>
      </div>
      <span class="na-badge na-badge--sample">本地全链路模拟</span>
    </div>
    <div class="sql-flow" aria-label="查询链路">
      <span>输入运营问题</span><span aria-hidden="true">→</span> <span>Python Text2SQL</span><span aria-hidden="true">→</span> <span>SQL Guard</span><span aria-hidden="true">→</span>
      <span>本地只读数据库</span><span aria-hidden="true">→</span><span>真实查询结果</span>
    </div>
    <p class="na-muted sql-footnote">
      数据由项目生成，模型使用隔离的本地规则 Port。
      仅支持场景示例中的新闻指标、栏目和内容类型；未知条件会拒绝，不会忽略。
      时间请在页面选择。企业接口接入后复用同一契约。
    </p>
  </section>

  <section v-if="!configuration" class="na-card">
    <p class="na-muted">
      {{ pending ? '正在加载场景、冻结表结构与本地样本数据…' : '尚未读取本地查询配置。' }}
    </p>
    <p v-if="error" class="na-message na-message--error" role="alert">{{ error }}</p>
    <button class="na-btn" type="button" :disabled="pending" @click="loadConfiguration">
      重新加载
    </button>
  </section>

  <template v-else>
    <section class="na-card">
      <div class="na-card__head">
        <h2>1. 输入查询问题</h2>
        <span class="na-muted">{{ configuration.dataset.news_count }} 条新闻 ·
          {{ configuration.dataset.metric_row_count }} 条聚合记录</span>
      </div>
      <div class="na-grid na-grid--split">
        <div>
          <div class="na-field">
            <label for="sql-scenario">Text2SQL 场景</label>
            <select
              id="sql-scenario"
              v-model="scenarioId"
              class="na-select"
              :disabled="pending"
              @change="changeScenario"
            >
              <option
                v-for="scenario in configuration.scenarios"
                :key="scenario.id"
                :value="scenario.id"
              >
                {{ scenario.name }}
              </option>
            </select>
            <span class="na-muted sql-footnote">{{ selectedScenario?.description }}</span>
          </div>
          <div class="na-field">
            <label for="sql-question">你想了解什么？</label>
            <textarea
              id="sql-question"
              v-model="question"
              class="na-textarea"
              rows="4"
              :disabled="pending"
              maxlength="1000"
              placeholder="例如：按点击量查询排名前 10 的新闻"
            />
          </div>
          <div v-if="selectedScenario?.sample_questions.length" class="sql-examples">
            <span class="na-muted">试一试</span>
            <button
              v-for="sample in selectedScenario.sample_questions"
              :key="sample"
              class="na-btn sql-example"
              type="button"
              :disabled="pending"
              @click="question = sample"
            >
              {{ sample }}
            </button>
          </div>
        </div>
        <div class="sql-window">
          <h3>查询范围</h3>
          <p class="na-muted sql-footnote">
            时间按浏览器本地时区显示。默认覆盖固定样本窗口，可重复演示相同结果。
          </p>
          <div class="na-field">
            <label for="sql-window-start">开始时间（包含）</label>
            <input
              id="sql-window-start"
              v-model="windowStart"
              type="datetime-local"
              step="3600"
              class="na-input"
              :disabled="pending"
            >
          </div>
          <div class="na-field">
            <label for="sql-window-end">结束时间（不包含）</label>
            <input
              id="sql-window-end"
              v-model="windowEnd"
              type="datetime-local"
              step="3600"
              class="na-input"
              :disabled="pending"
            >
          </div>
          <button class="na-btn" type="button" :disabled="pending" @click="restoreDatasetWindow">
            恢复样本窗口
          </button>
        </div>
      </div>
      <div class="na-row na-row--end sql-actions">
        <span class="na-muted">修改问题、场景或时间范围后，需要重新生成预览。</span>
        <button
          class="na-btn na-btn--primary"
          type="button"
          :disabled="pending || !canPreview"
          @click="generatePreview"
        >
          {{ action === 'preview' ? '正在生成并校验 SQL…' : '生成 SQL 预览' }}
        </button>
      </div>
      <p v-if="validationError || error" class="na-message na-message--error" role="alert">
        {{ validationError || error }}
      </p>
    </section>

    <section v-if="preview" class="na-card">
      <div class="na-card__head">
        <div>
          <h2>2. 查看并执行 SQL</h2>
          <p class="na-muted">{{ preview.explanation }}</p>
        </div>
        <span class="na-badge na-badge--live">只读校验通过</span>
      </div>
      <pre class="na-log sql-code">{{ preview.sql }}</pre>
      <div class="na-grid na-grid--split sql-preview-details">
        <NaJsonBlock :value="preview.parameters" label="服务端绑定参数" :collapsed-height="200" />
        <dl class="na-defs sql-identity">
          <div>
            <dt>SQL SHA-256</dt>
            <dd class="na-mono">{{ preview.sql_hash }}</dd>
          </div>
          <div>
            <dt>模型 Provider / 请求 ID</dt>
            <dd class="na-mono">
              {{ preview.model_provider }} / {{ preview.model_request_id ?? '确定性模板' }}
            </dd>
          </div>
          <div>
            <dt>预览有效期</dt>
            <dd>{{ displayDate(preview.expires_at) }}</dd>
          </div>
        </dl>
      </div>
      <div class="na-row na-row--end">
        <span class="na-muted">执行服务端保存的 SQL 快照；页面不提交可编辑 SQL。</span>
        <button
          class="na-btn na-btn--primary"
          type="button"
          :disabled="pending"
          @click="executeQuery"
        >
          {{ action === 'execute' ? '正在执行只读查询…' : '执行查询' }}
        </button>
      </div>
    </section>

    <section v-if="result" class="na-card" aria-live="polite">
      <div class="na-card__head">
        <div>
          <h2>3. 查询结果</h2>
          <p class="na-muted">{{ result.summary }}</p>
        </div>
        <span class="na-badge na-badge--live">{{ result.row_count }} 行 · {{ result.elapsed_ms }} ms</span>
      </div>
      <div v-if="result.truncated" class="na-notice">
        结果达到返回行数上限，仅展示允许范围内的数据。
      </div>
      <div class="sql-table-wrap">
        <table class="na-table sql-result-table">
          <thead>
            <tr>
              <th v-for="column in result.columns" :key="column">
                {{ columnLabel(column) }}
                <div class="na-cell__sub">{{ column }}</div>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, index) in result.rows" :key="index">
              <td v-for="column in result.columns" :key="column">
                {{ cellText(row[column], column) }}
              </td>
            </tr>
            <tr v-if="!result.rows.length">
              <td :colspan="Math.max(result.columns.length, 1)" class="na-empty">
                当前范围没有匹配记录。可恢复样本窗口或调整查询问题。
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-if="stages.length" class="na-card">
      <div class="na-card__head">
        <h2>执行链路</h2>
        <span class="na-muted">每个阶段的校验、耗时与尝试次数</span>
      </div>
      <div class="sql-table-wrap">
        <table class="na-table sql-trace-table">
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
            <tr v-for="(stage, index) in stages" :key="`${index}-${stage.name}`">
              <td class="sql-nowrap">{{ index + 1 }}. {{ stageLabel(stage.name) }}</td>
              <td>
                <span :class="stageClass(stage)">{{ statusLabel(stage.status) }}</span>
              </td>
              <td>{{ stage.detail }}</td>
              <td class="sql-nowrap">{{ stage.elapsed_ms }} ms</td>
              <td>{{ stage.attempts }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="na-card">
      <div class="na-card__head">
        <div>
          <h2>数据库格式契约</h2>
          <p class="na-muted">冻结版本与 SHA-256 由后端校验。表结构变更需另建新版本。</p>
        </div>
        <button class="na-btn" type="button" @click="downloadSchema">下载冻结 Markdown</button>
      </div>
      <dl class="na-defs">
        <div>
          <dt>Schema 版本</dt>
          <dd class="na-mono">{{ configuration.schema_version }}</dd>
        </div>
        <div>
          <dt>Schema SHA-256</dt>
          <dd class="na-mono">{{ configuration.schema_sha256 }}</dd>
        </div>
      </dl>
      <details class="sql-details">
        <summary>查看字段格式、指标口径与约束</summary>
        <pre class="na-log sql-contract">{{ configuration.schema_markdown }}</pre>
      </details>
    </section>

    <section class="na-card">
      <div class="na-card__head">
        <div>
          <h2>Text2SQL 场景配置模板</h2>
          <p class="na-muted">按 YAML 模板填写场景；Prompt 和模型版本在后端模型配置中单独绑定。</p>
        </div>
        <span class="na-badge">YAML</span>
      </div>
      <div class="na-notice">
        此编辑区用于准备配置文件，不会即时改变当前运行场景。模型接口密钥请通过后端环境变量引用。
      </div>
      <details class="sql-details">
        <summary>展开编辑与下载模板</summary>
        <label class="na-muted sql-template-label" for="sql-configuration">场景 YAML（可编辑）</label>
        <textarea
          id="sql-configuration"
          v-model="configurationTemplate"
          class="na-textarea na-mono sql-template"
          rows="20"
          spellcheck="false"
        />
        <div class="na-row na-row--end sql-actions">
          <button
            class="na-btn"
            type="button"
            @click="configurationTemplate = configuration.configuration_template"
          >
            恢复模板
          </button>
          <button
            class="na-btn na-btn--primary"
            type="button"
            :disabled="!configurationTemplate.trim()"
            @click="downloadTemplate"
          >
            下载 YAML 配置
          </button>
        </div>
      </details>
    </section>
  </template>
</template>

<style scoped>
.sql-intro p,
.sql-card p {
  margin: 4px 0;
}
.sql-flow {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 10px 12px;
  border-radius: var(--na-radius-sm);
  background: var(--na-accent-weak);
  color: var(--na-accent);
}
.sql-footnote {
  font-size: 12px;
}
.sql-intro .sql-footnote {
  margin-top: 10px;
}
.sql-window {
  padding: 12px;
  background: var(--na-bg);
  border: 1px solid var(--na-border);
  border-radius: var(--na-radius-sm);
}
.sql-examples {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
}
.sql-example {
  padding: 4px 8px;
  font-size: 12px;
  text-align: left;
}
.sql-actions {
  margin-top: 14px;
}
.sql-actions > .na-muted {
  margin-right: auto;
  font-size: 12px;
}
.sql-code {
  max-height: 400px;
  white-space: pre-wrap;
  word-break: break-word;
}
.sql-preview-details {
  margin: 10px 0;
}
.sql-identity {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.sql-table-wrap {
  overflow-x: auto;
}
.sql-result-table td {
  white-space: nowrap;
}
.sql-result-table tbody tr,
.sql-trace-table tbody tr {
  cursor: default;
}
.sql-nowrap {
  white-space: nowrap;
}
.sql-details {
  margin-top: 12px;
}
.sql-details summary {
  cursor: pointer;
  color: var(--na-accent);
}
.sql-contract {
  margin-top: 12px;
  max-height: 520px;
  white-space: pre-wrap;
  word-break: break-word;
}
.sql-template-label {
  display: block;
  margin: 12px 0 4px;
  font-size: 12px;
}
.sql-template {
  line-height: 1.65;
  white-space: pre;
  min-height: 320px;
}
</style>
