import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { compileScript, parse } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
import * as vue from 'vue'

const source = readFileSync(new URL('../src/components/hotnews/NewsReaderProfilePanel.vue', import.meta.url), 'utf8')
const formatSource = readFileSync(new URL('../src/utils/format.ts', import.meta.url), 'utf8')
function load(source, imports = {}) {
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  })
  const exports = {}
  new Function('require', 'exports', outputText)((name) => {
    assert.ok(Object.hasOwn(imports, name), `Unexpected import: ${name}`)
    return imports[name]
  }, exports)
  return exports
}
const component = load(compileScript(parse(source).descriptor, {
  id: 'news-reader-profile', inlineTemplate: true,
}).content, {
  vue, '@/utils/format': load(formatSource),
  '@/components/NaJsonBlock.vue': { default: { render: () => vue.h('div', 'aggregate evidence') } },
}).default

function summary(overrides = {}) {
  const interest = {
    lookback_hours: 720, profiled_reader_count: 5, unknown_reader_count: 3, cold_start_reader_count: 2,
    baseline_profiled_count: 50,
    topics: [{ topic_id: 'tech.nature', reader_count: 5, share_all: '0.62500000', share_profiled: '1',
      baseline_reader_count: 25, baseline_profiled_count: 50, baseline_share: '0.5', lift: '2', low_sample: false }],
  }
  return { news_id: 'news-1', source_mode: 'synthetic', taxonomy_version: 'topics-v1',
    profile_policy_version: 'profiles-v1', reader_count: 8, long_term: structuredClone(interest),
    short_term: { ...structuredClone(interest), lookback_hours: 24 }, profile_cutoff: '2026-10-14T11:00:00Z',
    trigger_article_excluded: true, count_method: 'exact_synthetic_distinct', error_bound: null,
    status: 'complete', limitations: [], ...overrides }
}

const render = (value, trace = { topic_names: { 'tech.nature': '科技 / 自然' } }) =>
  renderToString(vue.createSSRApp(component, { summary: value, trace }))

test('renders per-news reader coverage, frozen Chinese topics and exact percentage strings', async () => {
  const html = await render(summary())
  for (const expected of ['该新闻访问人群', '合成样例', '长期兴趣', '短期兴趣', '科技 / 自然',
    '62.50%', '2 倍', '未知 3 人', '冷启动 2 人', '已排除当前文章', '占比不可相加', '可触达人数需另行核验']) {
    assert.ok(html.includes(expected), expected)
  }
})

test('zero cohort shows empty state, and low sample never displays a lift claim', async () => {
  const empty = summary({ reader_count: 0, status: 'empty' })
  const emptyHtml = await render(empty)
  assert.match(emptyHtml, /暂无模拟读者/)
  assert.doesNotMatch(emptyHtml, /<table/)
  const low = summary()
  for (const value of [low.long_term, low.short_term]) { value.topics[0].lift = null; value.topics[0].low_sample = true }
  const lowHtml = await render(low)
  assert.match(lowHtml, /样本不足/)
  assert.doesNotMatch(lowHtml, /2 倍/)
})

test('enterprise approximate counts show error bound and topic names stay escaped', async () => {
  const html = await render(summary({ source_mode: 'enterprise', count_method: 'enterprise_approx_distinct', error_bound: '0.02' }),
    { topic_names: { 'tech.nature': '<script>alert(1)</script>' } })
  assert.match(html, /企业聚合/)
  assert.match(html, /误差上界：2.00%/)
  assert.doesNotMatch(html, /<script>/)
  assert.match(html, /&lt;script&gt;/)
  const historical = await render(summary(), null)
  assert.match(historical, /tech.nature/)
  const emptyEnterprise = await render(summary({ source_mode: 'enterprise', reader_count: 0, status: 'empty' }))
  assert.match(emptyEnterprise, /暂无读者/)
  assert.doesNotMatch(emptyEnterprise, /暂无模拟读者/)
})
