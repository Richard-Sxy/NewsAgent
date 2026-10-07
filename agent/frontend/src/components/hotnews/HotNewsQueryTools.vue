<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { hotNewsApi } from '@/api/hotNews'
import type { SqlAssistantConfig, SqlAssistantPreviewRequest } from '@/api/sqlAssistant'
import { useAsyncTask } from '@/composables/useAsyncTask'
import { downloadBlob } from '@/utils/download'

const props = defineProps<{ running: boolean }>()
const emit = defineEmits<{ run: [request: SqlAssistantPreviewRequest] }>()
const configuration = ref<SqlAssistantConfig | null>(null)
const question = ref('')
const scenarioId = ref('')
const windowStart = ref('')
const configurationTemplate = ref('')
const validationError = ref('')
const { pending, error, run } = useAsyncTask()
const disabled = computed(() => pending.value || props.running)
const scenarios = computed(
  () =>
    configuration.value?.scenarios.filter((scenario) => scenario.result_mode === 'ranking') ?? [],
)
const selectedScenario = computed(() =>
  scenarios.value.find((scenario) => scenario.id === scenarioId.value),
)
const windowEnd = computed(() => {
  const start = new Date(windowStart.value)
  return Number.isNaN(start.getTime())
    ? ''
    : localDateInput(new Date(start.getTime() + 3_600_000).toISOString())
})

function localDateInput(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (number: number) => String(number).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

async function loadConfiguration(): Promise<void> {
  if (disabled.value) return
  const value = await run(() => hotNewsApi.localSqlConfig(), {
    failure: '热点 SQL 工具配置读取失败',
  })
  if (!value) return
  configuration.value = value
  scenarioId.value = scenarios.value[0]?.id ?? ''
  question.value = scenarios.value[0]?.sample_questions[0] ?? ''
  windowStart.value = localDateInput(value.dataset.window_start)
  configurationTemplate.value = value.configuration_template
}

function changeScenario(): void {
  question.value = selectedScenario.value?.sample_questions[0] ?? ''
  validationError.value = ''
}

function startAgent(): void {
  if (disabled.value) return
  validationError.value = ''
  const start = new Date(windowStart.value)
  if (!question.value.trim() || !selectedScenario.value) {
    validationError.value = '请选择查询场景并输入热点筛选问题。'
    return
  }
  if (
    Number.isNaN(start.getTime()) ||
    start.getMinutes() ||
    start.getSeconds() ||
    start.getMilliseconds()
  ) {
    validationError.value = '请选择整点开始时间，本地热点运行固定查询一个小时。'
    return
  }
  emit('run', {
    question: question.value.trim(),
    scenario_id: scenarioId.value,
    window_start: start.toISOString(),
    window_end: new Date(start.getTime() + 3_600_000).toISOString(),
  })
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
    'hot-news-text2sql-scenes.yml',
  )
}

onMounted(loadConfiguration)
</script>

<template>
  <section id="query-tools" class="na-card query-tools">
    <div class="na-card__head">
      <div>
        <h2>运行热点 Agent</h2>
        <p class="na-muted">
          输入候选新闻筛选条件，Text2SQL 作为热点 Agent 的取数工具参与本次运行。
        </p>
      </div>
      <span class="na-badge na-badge--sample">本地全链路模拟</span>
    </div>
    <div class="query-tools__flow">
      <span>运营问题</span><span aria-hidden="true">→</span><span>Text2SQL 与 SQL Guard</span>
      <span aria-hidden="true">→</span><span>聚合指标</span><span aria-hidden="true">→</span>
      <span>Python 热度排序</span><span aria-hidden="true">→</span><span>证据与 Agent 分析</span>
    </div>
    <p class="na-muted query-tools__note">
      指标与模型为隔离模拟。SQL 负责候选筛选，最终热度和榜单由 Python HotNewsRanker 确定性计算。
    </p>
    <p v-if="configuration?.dataset.dataset_profile === 'public-headlines-v3'" class="na-notice">
      新闻标题来自公开报道（{{ configuration.dataset.headline_date_start }} 至 {{ configuration.dataset.headline_date_end }}）。
      行为指标与基线使用10月3日的模拟窗口，与标题发布日期相互独立，不能解释为这些报道的真实用户行为。
    </p>
    <div v-if="!configuration">
      <p class="na-muted">
        {{ pending ? '正在读取热点查询工具配置…' : '本地热点查询工具配置尚未加载。' }}
      </p>
      <p v-if="error" class="na-message na-message--error" role="alert">{{ error }}</p>
      <button class="na-btn" type="button" :disabled="disabled" @click="loadConfiguration">
        重新加载工具配置
      </button>
    </div>
    <template v-else>
      <div class="na-grid na-grid--split">
        <div>
          <div class="na-field">
            <label for="hot-news-sql-scene">候选筛选场景</label>
            <select
              id="hot-news-sql-scene"
              v-model="scenarioId"
              class="na-select"
              :disabled="disabled"
              @change="changeScenario"
            >
              <option v-for="scenario in scenarios" :key="scenario.id" :value="scenario.id">
                {{ scenario.name }}
              </option>
            </select>
            <span class="na-muted query-tools__note">{{ selectedScenario?.description }}</span>
          </div>
          <div class="na-field">
            <label for="hot-news-sql-question">热点筛选问题</label>
            <textarea
              id="hot-news-sql-question"
              v-model="question"
              class="na-textarea"
              rows="3"
              maxlength="1000"
              :disabled="disabled"
              placeholder="例如：查询点击量最高的前10条新闻"
            />
          </div>
          <div class="query-tools__examples">
            <button
              v-for="sample in selectedScenario?.sample_questions ?? []"
              :key="sample"
              class="na-btn"
              type="button"
              :disabled="disabled"
              @click="question = sample"
            >
              {{ sample }}
            </button>
          </div>
        </div>
        <div class="query-tools__window">
          <h3>热点指标窗口</h3>
          <p class="na-muted query-tools__note">
            固定一个小时，按浏览器本地时区显示。租户由后端身份上下文绑定。
          </p>
          <div class="na-field">
            <label for="hot-news-window-start">开始时间（包含）</label><input
              id="hot-news-window-start"
              v-model="windowStart"
              class="na-input"
              type="datetime-local"
              step="3600"
              :disabled="disabled"
            >
          </div>
          <div class="na-field">
            <label for="hot-news-window-end">结束时间（不包含）</label><input
              id="hot-news-window-end"
              :value="windowEnd"
              class="na-input"
              type="datetime-local"
              readonly
            >
          </div>
          <button
            class="na-btn"
            type="button"
            :disabled="disabled"
            @click="windowStart = localDateInput(configuration.dataset.window_start)"
          >
            恢复演示窗口
          </button>
        </div>
      </div>
      <p v-if="validationError" class="na-message na-message--error" role="alert">
        {{ validationError }}
      </p>
      <div class="na-row na-row--end query-tools__actions">
        <span class="na-muted">SQL、指标输入、热点榜单和分析报告将绑定同一次运行。</span>
        <button
          class="na-btn na-btn--primary"
          type="button"
          :disabled="disabled || !scenarios.length || !question.trim()"
          @click="startAgent"
        >
          {{ running ? 'Agent 运行中：SQL 取数 → 热度排序 → 证据分析…' : '启动热点 Agent' }}
        </button>
      </div>
      <details class="query-tools__details">
        <summary>数据库冻结契约与 Text2SQL 场景模板</summary>
        <dl class="na-defs query-tools__metadata">
          <div>
            <dt>Schema 版本</dt>
            <dd class="na-mono">{{ configuration.schema_version }}</dd>
          </div>
          <div>
            <dt>Schema SHA-256</dt>
            <dd class="na-mono">{{ configuration.schema_sha256 }}</dd>
          </div>
        </dl>
        <div class="na-row query-tools__actions">
          <button class="na-btn" type="button" @click="downloadSchema">下载冻结 Markdown</button><button class="na-btn" type="button" :disabled="disabled" @click="loadConfiguration">
            重新读取场景配置
          </button>
        </div>
        <details class="query-tools__details">
          <summary>查看数据库字段与指标口径</summary>
          <pre class="na-log query-tools__contract">{{ configuration.schema_markdown }}</pre>
        </details>
        <p class="na-muted query-tools__note">
          模板可编辑并下载，保存到后端场景配置文件后重新加载。编辑区不会即时改变当前
          Agent；模型密钥通过后端环境变量引用。
        </p>
        <label class="na-muted query-tools__note" for="hot-news-sql-template">场景 YAML 模板</label>
        <textarea
          id="hot-news-sql-template"
          v-model="configurationTemplate"
          class="na-textarea na-mono query-tools__template"
          rows="16"
          spellcheck="false"
        />
        <div class="na-row na-row--end query-tools__actions">
          <button
            class="na-btn"
            type="button"
            @click="configurationTemplate = configuration.configuration_template"
          >
            恢复模板
          </button><button
            class="na-btn"
            type="button"
            :disabled="!configurationTemplate.trim()"
            @click="downloadTemplate"
          >
            下载场景 YAML
          </button>
        </div>
      </details>
    </template>
  </section>
</template>

<style scoped>
.query-tools {
  scroll-margin-top: 16px;
}
.query-tools__flow {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 10px 12px;
  background: var(--na-accent-weak);
  color: var(--na-accent);
  border-radius: var(--na-radius-sm);
}
.query-tools__note {
  font-size: 12px;
}
.query-tools__examples {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.query-tools__examples button {
  padding: 4px 8px;
  font-size: 12px;
  text-align: left;
}
.query-tools__window {
  padding: 12px;
  background: var(--na-bg);
  border: 1px solid var(--na-border);
  border-radius: var(--na-radius-sm);
}
.query-tools__actions {
  margin-top: 12px;
}
.query-tools__actions > span {
  margin-right: auto;
  font-size: 12px;
}
.query-tools__details {
  margin-top: 12px;
}
.query-tools__details summary {
  cursor: pointer;
  color: var(--na-accent);
}
.query-tools__metadata,
.query-tools__contract {
  margin-top: 12px;
}
.query-tools__contract {
  max-height: 440px;
  white-space: pre-wrap;
  word-break: break-word;
}
.query-tools__template {
  margin-top: 4px;
  line-height: 1.65;
  white-space: pre;
}
</style>
