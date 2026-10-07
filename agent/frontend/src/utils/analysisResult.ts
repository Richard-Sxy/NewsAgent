import type { ConversationToolTrace } from '@/api/conversations'
import { formatDecimal, formatRatioPercent } from '@/utils/format'

type ObjectValue = Record<string, unknown>
export interface AnalysisField { label: string; value: string }
export interface AnalysisSource {
  runId: string; windowStart: string; windowEnd: string; bundle: string
  rowCount: string; hash: string; provenance: AnalysisField[]
}
export interface AnalysisResultView {
  title: string; metric: string; fields: AnalysisField[]; columns: string[]; rows: string[][]
  source: AnalysisSource; reference: AnalysisSource | null; execution: AnalysisField[]
  limitations: string[]; notes: string[]; unsafeInteger: boolean
}
const OPERATIONS: Record<string, string> = {
  overview: '指标概况', distribution: '指标分布', compare: '高低对比',
  quality: '数据质量', baseline: '已存基线对比', trend: '两窗口趋势',
}
const METRICS: Record<string, string> = {
  impressions: '曝光量', clicks: '点击量', ctr: '点击率（%）', hot_score: '热度',
  effective_consumptions: '有效消费', interactions: '互动量', total_duration_seconds: '消费时长（秒）',
}
const LIMITS: Record<string, string> = {
  timeout_seconds: '计算期限（秒）', max_rows: '总输入行数上限', max_input_bytes: '输入上限（字节）',
  max_output_bytes: '输出上限（字节）', max_concurrency: '并发上限', memory_mb: '内存上限（MiB）',
  os_resource_limits_enforced: '操作系统资源限制',
}
const object = (value: unknown): ObjectValue | null =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? value as ObjectValue : null
const text = (value: unknown): string => typeof value === 'string' ? value : '未知'
const strings = (value: unknown): string[] => Array.isArray(value)
  ? value.filter((item): item is string => typeof item === 'string') : []

/** 只格式化已存结果，不在浏览器重算比率、趋势或修复 JSON 数字精度。 */
export function projectAnalysisResult(trace: ConversationToolTrace): AnalysisResultView | null {
  if (trace.name !== 'analyze_hot_news_data' || trace.status !== 'completed') return null
  const report = object(trace.result)
  const analysis = object(report?.analysis)
  const execution = object(report?.execution)
  const sourceRecord = object(report?.source)
  const operation = report?.operation
  const metric = report?.metric
  if (!report || !analysis || !execution || !sourceRecord
      || (report.schema_version !== '1.0' && report.schema_version !== '2.0')
      || typeof operation !== 'string' || !OPERATIONS[operation]
      || typeof metric !== 'string' || !METRICS[metric]
      || !['process', 'docker'].includes(String(execution.backend))) return null
  let unsafeInteger = false
  function integer(value: unknown): string {
    if (value === null || value === undefined) return '—'
    if (typeof value === 'string' && /^-?\d+$/.test(value) && value.length <= 128) return formatDecimal(value, 0)
    if (typeof value === 'number' && Number.isSafeInteger(value)) return String(value)
    if (typeof value === 'number' && !Number.isSafeInteger(value)) {
      unsafeInteger = true
      return '见权威答复'
    }
    return '未知'
  }
  function decimal(value: unknown, ratio = metric === 'ctr'): string {
    if (value === null || value === undefined) return '—'
    return typeof value === 'string' && value.length <= 128
      && /^-?\d+(?:\.\d+)?$/.test(value)
      ? ratio ? formatRatioPercent(value) : formatDecimal(value, metric === 'hot_score' ? 4 : 2) : '未知'
  }
  function source(value: ObjectValue): AnalysisSource | null {
    if (typeof value.run_id !== 'string' || typeof value.window_start !== 'string'
        || typeof value.window_end !== 'string' || typeof value.bundle_version !== 'string'
        || value.scope !== 'ranked_news_only' || typeof value.snapshot_sha256 !== 'string'
        || !/^[0-9a-f]{64}$/.test(value.snapshot_sha256)) return null
    const provenance = object(value.provenance)
    return {
      runId: value.run_id, windowStart: value.window_start, windowEnd: value.window_end,
      bundle: value.bundle_version, rowCount: integer(value.row_count), hash: value.snapshot_sha256,
      provenance: provenance ? [
        { label: '工作流版本', value: text(provenance.workflow_version) },
        { label: '筛选规则指纹', value: text(provenance.selection_scope_sha256) },
      ] : [],
    }
  }
  const current = source(sourceRecord)
  const referenceRecord = object(sourceRecord.reference)
  const reference = referenceRecord ? source(referenceRecord) : null
  if (!current || (operation === 'trend' && !reference)) return null
  const fields: AnalysisField[] = []
  const rows: string[][] = []
  let columns: string[] = []
  const add = (label: string, value: string) => fields.push({ label, value })
  if (operation === 'overview') {
    const totals = object(analysis.totals)
    for (const [key, label] of Object.entries(METRICS)) {
      if (key !== 'ctr' && key !== 'hot_score') add(label, integer(totals?.[key]))
    }
    add('按曝光加权点击率（%）', decimal(analysis.weighted_ctr, true))
  } else if (operation === 'distribution') {
    add('样本数', integer(analysis.count))
    for (const [key, label] of [['min', '最小值'], ['max', '最大值'], ['mean', '均值'],
      ['median', '中位数'], ['p90', 'P90']]) add(label!, decimal(analysis[key!]))
  } else if (operation === 'compare') {
    for (const [key, label] of [['highest', '最高'], ['lowest', '最低']]) {
      const item = object(analysis[key!])
      add(`${label}新闻`, item ? text(item.news_id) : '—')
      add(`${label}值`, item ? decimal(item.value) : '—')
    }
    add('绝对差值', decimal(analysis.absolute_difference))
    add('相对变化（%）', decimal(analysis.relative_change, true))
  } else if (operation === 'quality') {
    for (const [key, label] of [['zero_impression_news_ids', '零曝光新闻'],
      ['ctr_mismatch_news_ids', '点击率不一致新闻'], ['clicks_above_impressions_news_ids', '点击超过曝光新闻']]) {
      add(label!, Array.isArray(analysis[key!]) ? strings(analysis[key!]).join('、') || '无' : '未知')
    }
  } else {
    columns = ['新闻', '当前值', operation === 'baseline' ? '基线值' : '参考窗口值', '绝对变化', '相对变化（%）']
    if (operation === 'baseline') {
      columns.push('基线样本数', '参考版本', '状态')
      add('已比较新闻数', integer(analysis.compared_count))
      add('缺失基线', strings(analysis.missing_baseline_news_ids).join('、') || '无')
      add('缺失指标', strings(analysis.missing_metric_news_ids).join('、') || '无')
    } else {
      add('交集新闻数', integer(analysis.matched_news_count))
      add('窗口间隔（秒）', integer(analysis.gap_seconds))
      add('新增榜单新闻', strings(analysis.added_news_ids).join('、') || '无')
      add('移出榜单新闻', strings(analysis.removed_news_ids).join('、') || '无')
      const aggregate = object(analysis.aggregate)
      const methods: Record<string, string> = { sum: '交集合计', mean: '交集均值', weighted_ctr: '交集按曝光加权点击率' }
      add('汇总口径', methods[String(aggregate?.method)] ?? '未知')
      for (const [key, label] of [['current_value', '交集当前值'], ['reference_value', '交集参考值'],
        ['absolute_change', '交集绝对变化'], ['relative_change', '交集相对变化（%）']]) {
        add(label!, decimal(aggregate?.[key!], key === 'relative_change' || metric === 'ctr'))
      }
    }
    if (Array.isArray(analysis.items)) for (const value of analysis.items) {
      const item = object(value)
      if (!item) continue
      const row = [text(item.news_id), decimal(item.current_value), decimal(item.reference_value),
        decimal(item.absolute_change), decimal(item.relative_change, true)]
      if (operation === 'baseline') row.push(item.sample_count === null ? '—' : integer(item.sample_count),
        item.reference_version === null ? '—' : text(item.reference_version),
        ({ compared: '已比较', missing_baseline: '缺失基线', missing_metric: '缺失指标' } as Record<string, string>)[String(item.status)] ?? '未知')
      rows.push(row)
    }
  }
  const audit: AnalysisField[] = [{ label: '实际计算后端', value: String(execution.backend) }]
  audit.push({ label: '传输', value: execution.transport === 'service' ? 'service' : '未声明' })
  audit.push({ label: '计算耗时（毫秒）', value: integer(execution.elapsed_ms) },
    { label: '请求 SHA-256', value: text(execution.input_sha256) },
    { label: '算法版本', value: text(report.algorithm_version) })
  const limits = object(execution.limits)
  for (const [key, label] of Object.entries(LIMITS)) {
    const value = limits?.[key]
    audit.push({ label, value: typeof value === 'boolean' ? (value ? '已强制' : '未强制')
      : key === 'timeout_seconds' && typeof value === 'number' && Number.isFinite(value) && value > 0
        ? String(value) : integer(value) })
  }
  return { title: OPERATIONS[operation]!, metric: METRICS[metric]!, fields, columns, rows,
    source: current, reference, execution: audit, limitations: strings(report.limitations),
    notes: strings(analysis.notes), unsafeInteger }
}
