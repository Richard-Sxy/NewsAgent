import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import { compileScript, parse } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import * as vue from 'vue'

const source = readFileSync(new URL('../src/utils/format.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
})
const format = {}
new Function('exports', outputText)(format)

test('reference and mean decimals round exactly to at most two places, including negative and carry cases', () => {
  for (const [input, output] of [['1.005', '1.01'], ['-1.005', '-1.01'], ['999.999', '1000'],
    ['12.3400000000000000001', '12.34'], ['0.000001', '0'], ['-0.0049', '0'],
    ['9007199254740993.995', '9007199254740994'], ['001.2300', '1.23']]) {
    assert.equal(format.formatDecimal(input), output)
  }
  assert.equal(format.formatDecimal('0.123456789123456789', 4), '0.1235')
  assert.equal(format.formatDecimal(1e-7), '0')
})

test('ratio percentages use two places with exact decimal point shift and no floating-point coercion', () => {
  for (const [input, output] of [['0.12345', '12.35%'], ['-0.12345', '-12.35%'],
    ['0.99999999999999', '100.00%'], ['0', '0.00%'], ['0.00005', '0.01%'],
    ['9007199254740993.00005', '900719925474099300.01%']]) {
    assert.equal(format.formatRatioPercent(input), output)
  }
  assert.equal(format.formatRatioPercent(1e-7), '0.00%')
  assert.equal(format.formatPercent(99.12345), '99.12%')
})

test('missing and invalid values stay missing while unsafe JSON integers refer to the authoritative result', () => {
  for (const value of [null, undefined, NaN, Infinity, '-Infinity', '1e-3', '', 'bad']) {
    assert.equal(format.formatDecimal(value), '—')
    assert.equal(format.formatRatioPercent(value), '—')
  }
  assert.equal(format.formatDecimal(Number.MAX_SAFE_INTEGER + 1), '见权威答复')
  assert.equal(format.formatRatioPercent(Number.MAX_SAFE_INTEGER + 1), '见权威答复')
  assert.equal(format.formatDecimal('9007199254740993'), '9007199254740993')
})

test('SQL cells format by semantic column and keep identifiers, headlines and source text intact', () => {
  assert.equal(format.formatMetricCell('0.012345', 'ctr'), '1.23%')
  assert.equal(format.formatMetricCell('0.12345678', 'hot_score'), '0.1235')
  assert.equal(format.formatMetricCell('0.012345', 'baseline_ctr'), '1.23%')
  assert.equal(format.formatMetricCell('0.12345678', 'baseline_hot_score'), '0.1235')
  assert.equal(format.formatMetricCell('120.123456', 'baseline_clicks'), '120.12')
  assert.equal(format.formatMetricCell('9007199254740993', 'impressions'), '9007199254740993')
  for (const column of ['news_id', 'title', 'source', 'event_time']) {
    assert.equal(format.formatMetricCell('001.2345678', column), '001.2345678')
  }
  assert.equal(format.formatMetricCell(null, 'clicks'), '—')
})

test('the actual hot-news table renders the headline first and formats metric strings on display', async () => {
  const componentSource = readFileSync(new URL('../src/components/hotnews/HotNewsRankTable.vue', import.meta.url), 'utf8')
  const compiled = compileScript(parse(componentSource).descriptor, { id: 'formatted-rank-table', inlineTemplate: true }).content
  const { outputText } = ts.transpileModule(compiled, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  })
  const imports = { vue, '@/utils/format': format,
    '@/components/NaJsonBlock.vue': { default: vue.defineComponent({ render: () => null }) } }
  const exports = {}
  new Function('require', 'exports', outputText)((name) => {
    assert.ok(Object.hasOwn(imports, name), `Unexpected import: ${name}`)
    return imports[name]
  }, exports)
  const item = { rank: 1, news_id: 'scale-news-000001', title: '假期交通服务迎来新变化',
    metrics: { content_type: 'article', impressions: 100, clicks: 12, ctr: '0.123456789123456789' },
    hot_score: { score: '0.123456789', click_component: '0.1', consumption_component: '0.2',
      interaction_component: '0.3', growth_component: '0.4' },
    analysis: { overall_confidence: 0.98765 },
  }
  const original = structuredClone(item)
  const html = await renderToString(vue.createSSRApp({ render: () => vue.h(exports.default, { items: [item] }) }))
  assert.match(html, /class="na-cell__title"[^>]*>假期交通服务迎来新变化<\/div>/)
  assert.match(html, /class="na-cell__sub[^>]*title="scale-news-000001"/)
  assert.match(html, />12\.35%<\/td>/)
  assert.match(html, />0\.1235<\/span>/)
  assert.match(html, />98\.77%<\/span>/)
  assert.deepEqual(item, original)
})
