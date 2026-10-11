import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { compileScript, parse } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
import * as vue from 'vue'

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

const helper = load(readFileSync(new URL('../src/utils/relatedArticles.ts', import.meta.url), 'utf8'))
const panelSource = readFileSync(new URL('../src/components/conversation/RelatedArticlesPanel.vue', import.meta.url), 'utf8')
const viewSource = readFileSync(new URL('../src/views/ChatView.vue', import.meta.url), 'utf8')
const imports = { vue, '@/utils/relatedArticles': helper,
  '@/utils/format': { formatDateTime: (value) => `时间 ${value}` } }
const component = (inlineTemplate = true) => load(compileScript(parse(panelSource).descriptor,
  { id: 'related-articles-test', inlineTemplate }).content, imports).default

const article = (newsId = 'related-1', overrides = {}) => ({
  news_id: newsId, chunk_id: `chunk-${newsId}`, title: '相关新闻标题', excerpt: '相关新闻的内容片段。',
  source_url: 'https://news.example.com/article', publish_time: '2026-10-11T02:00:00+08:00',
  embedding_version: 'embedding-v1', ...overrides,
})
const source = (newsId = 'source-1', overrides = {}) => ({
  source_news_id: newsId, source_title: '热点新闻标题', source_run_id: 'run-1',
  status: 'completed', items: [article()], ...overrides,
})
const trace = (overrides = {}) => ({
  name: 'read_hot_news', status: 'completed', attempts: 1, arguments: {}, error_code: null,
  result: { run_id: 'run-1', items: [{ news_id: 'source-1' }], related_articles: [source()] },
  ...overrides,
})
const hotTrace = (sources, overrides = {}) => trace({ result: {
  run_id: 'run-1', items: sources.map((item) => ({ news_id: item.source_news_id })),
  related_articles: sources, ...overrides,
} })
const searchTrace = (overrides = {}) => trace({ name: 'search_knowledge', result: {
  query: '人工智能相关新闻', items: [article()], evidence_is_untrusted: true, ...overrides,
} })

test('saved hot-news results project bound source groups and bounded article evidence', () => {
  const [group] = helper.projectRelatedArticles([trace()])
  assert.equal(group.sourceNewsId, 'source-1')
  assert.equal(group.sourceRunId, 'run-1')
  assert.equal(group.sourceTitle, '热点新闻标题')
  assert.equal(group.items[0].publishTime, '2026-10-11T02:00:00+08:00')
  assert.equal(group.items[0].contentVersion, null)
  assert.equal(group.items[0].embeddingVersion, 'embedding-v1')
  assert.equal(group.items[0].chunkId, 'chunk-related-1')
  assert.equal(group.items[0].sourceUrl, 'https://news.example.com/article')
  assert.equal(helper.projectRelatedArticles([trace({ name: 'query_hot_news' })]).length, 1)
})

test('historical and malformed results do not create cards or parse assistant/model text', () => {
  for (const invalid of [[], null, [null], [trace({ result: null })],
    [trace({ name: 'read_hot_news', result: { items: [] } })],
    [trace({ name: 'unknown_tool' })], [trace({ status: 'failed' })], [trace({ status: 'denied' })],
    [trace({ status: 'running' })], [trace({ result: { assistant_content: JSON.stringify(source()) } })],
    [searchTrace({ query: null })], [searchTrace({ items: {} })]]) {
    assert.deepEqual(helper.projectRelatedArticles(invalid), [])
  }
  assert.deepEqual(helper.projectRelatedArticles([hotTrace([source('source-1', { status: 'processing' })])]), [])
  assert.deepEqual(helper.projectRelatedArticles([hotTrace([source('source-1', { items: {} })])]), [])
})

test('automatic articles must belong to the saved source list and exact run', () => {
  const invalid = [source('not-authorized'), source('source-1', { source_run_id: 'other-run' }),
    source('source-1', { source_run_id: null }), source('', { source_run_id: 'run-1' })]
  assert.deepEqual(helper.projectRelatedArticles([trace({ result: {
    run_id: 'run-1', items: [{ news_id: 'source-1' }], related_articles: invalid,
  } })]), [])
  assert.deepEqual(helper.projectRelatedArticles([hotTrace([source()], { run_id: null })]), [])
})

test('source exclusion, per-source article deduplication and three-by-three bounds', () => {
  const sources = Array.from({ length: 5 }, (_, index) => source(`source-${index}`, {
    items: [article(`source-${index}`), null, article('duplicate'), article('duplicate', { chunk_id: 'second-chunk' }),
      ...Array.from({ length: 5 }, (_, itemIndex) => article(`item-${itemIndex}`))],
  }))
  const groups = helper.projectRelatedArticles([hotTrace(sources)])
  assert.equal(groups.length, 3)
  for (const group of groups) {
    assert.deepEqual(group.items.map((item) => item.newsId), ['duplicate', 'item-0', 'item-1'])
    assert.equal(group.items[0].chunkId, 'chunk-duplicate')
    assert.ok(!group.items.some((item) => item.newsId === group.sourceNewsId))
  }
})

test('search results share source groups, retain source title, and recover failed automatic recalls', () => {
  const failed = source('source-1', { status: 'failed', items: [article('untrusted-partial')], notice: '检索超时' })
  const groups = helper.projectRelatedArticles([hotTrace([failed]), searchTrace({ source_news_id: 'source-1',
    query: '热点新闻标题', items: [article('source-1'), article('related-1'), article('related-2')],
    coverage_note: '离线新闻片段',
  }), hotTrace([source('source-1', { items: [article('related-2'), article('related-3')], notice: '离线新闻片段' })])])
  assert.equal(groups.length, 1)
  assert.equal(groups[0].status, 'completed')
  assert.equal(groups[0].sourceTitle, '热点新闻标题')
  assert.deepEqual(groups[0].items.map((item) => item.newsId), ['related-1', 'related-2', 'related-3'])
  assert.deepEqual(groups[0].notices, ['离线新闻片段'])
  assert.equal(helper.projectRelatedArticles([trace(), hotTrace([failed])])[0].status, 'completed')
})

test('ordinary knowledge searches work without a hot-news run and group by query or source', () => {
  const groups = helper.projectRelatedArticles([searchTrace(), searchTrace({ items: [article('related-2')] }),
    searchTrace({ source_news_id: 'external-source', query: '另一篇新闻的标题', items: [article('external-source'), article('related-3')] })])
  assert.equal(groups.length, 2)
  assert.equal(groups[0].sourceNewsId, null)
  assert.equal(groups[0].query, '人工智能相关新闻')
  assert.deepEqual(groups[0].items.map((item) => item.newsId), ['related-1', 'related-2'])
  assert.equal(groups[1].sourceTitle, '另一篇新闻的标题')
  assert.equal(groups[1].sourceNewsId, 'external-source')
  assert.deepEqual(groups[1].items.map((item) => item.newsId), ['related-3'])
})

test('unsafe URLs never become links while valid HTTP(S) sources remain navigable', () => {
  for (const unsafe of [null, 1, {}, '', '//news.example.com', 'javascript:alert(1)',
    'data:text/html,test', 'file:///tmp/news', 'https:news.example.com',
    'https://user:password@news.example.com/path', 'https://user@news.example.com',
    'https://news.example.com\n/redirect', 'https://news.example.com\t',
    'https://news.example.com/\u007f', 'https://news.example.com/\u0085',
    'https://news.example.com/' + 'x'.repeat(2048)]) {
    assert.equal(helper.safeRelatedArticleUrl(unsafe), null, String(unsafe))
    assert.equal(helper.projectRelatedArticles([searchTrace({ items: [article('one', { source_url: unsafe })] })])[0].items[0].sourceUrl, null)
  }
  assert.equal(helper.safeRelatedArticleUrl('HTTP://news.example.com/path'), 'http://news.example.com/path')
  assert.equal(helper.safeRelatedArticleUrl('https://news.example.com/?a=1&b=2'), 'https://news.example.com/?a=1&b=2')
})

test('Unicode excerpts stay within 600 characters and preview expands from 300 characters', () => {
  const excerpt = '🙂'.repeat(700)
  const projected = helper.projectRelatedArticles([searchTrace({ items: [article('one', { excerpt })] })])[0].items[0].excerpt
  assert.equal(Array.from(projected).length, 600)
  assert.equal(helper.relatedExcerptPreview(projected), '🙂'.repeat(300) + '…')
  assert.equal(helper.relatedExcerptPreview(projected, true), '🙂'.repeat(600))
  assert.equal(helper.relatedExcerptExpandable('文'.repeat(300)), false)
  assert.equal(helper.relatedExcerptExpandable('文'.repeat(301)), true)
  assert.equal(Array.from(helper.relatedExcerptPreview(excerpt, true)).length, 601)
})

test('missing fields and invalid dates retain honest unknown states without inventing metadata', () => {
  const raw = article('one', { title: {}, excerpt: {}, publish_time: '昨天', source_url: null })
  const [group] = helper.projectRelatedArticles([searchTrace({ items: [raw, { news_id: '' }, { news_id: 123 }] })])
  assert.equal(group.items.length, 1)
  assert.equal(group.items[0].title, '新闻 one')
  assert.equal(group.items[0].excerpt, '')
  assert.equal(group.items[0].publishTime, null)
  for (const date of [null, 'not-an-iso-date', '2026-10-11', '2026-10-11T02:00:00', '2026-99-99T00:00:00Z',
    '2026-02-30T00:00:00Z', '2026-02-29T00:00:00Z', '2026-10-11T24:00:00Z']) {
    assert.equal(helper.projectRelatedArticles([searchTrace({ items: [article('one', { publish_time: date })] })])[0].items[0].publishTime, null)
  }
})

test('Vue renders untrusted titles and excerpts as text with safe links and missing provenance labels', async () => {
  const html = await renderToString(vue.createSSRApp(component(), { tools: [hotTrace([source('source-1', {
    source_title: '<img src=x onerror=alert(1)>', items: [article('one', {
      title: '<script>alert(1)</script>', excerpt: '<img src=x onerror=alert(2)>',
      publish_time: null, source_url: 'javascript:alert(3)',
    }), article('two')],
  })])] }))
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/)
  assert.match(html, /&lt;img src=x onerror=alert\(2\)&gt;/)
  assert.ok(!html.includes('<script>'))
  assert.ok(!html.includes('<img'))
  assert.ok(!html.includes('href="javascript:'))
  assert.match(html, /target="_blank" rel="noopener noreferrer"/)
  assert.match(html, /发布时间：未知/)
  assert.match(html, /来源链接未提供/)
  assert.match(html, /片段不代表完整原文/)
  assert.ok(!html.includes('embedding-v1'))
})

test('empty completed and failed recalls render distinct notices; old histories render no panel', async () => {
  const html = await renderToString(vue.createSSRApp(component(), { tools: [hotTrace([
    source('source-1', { items: [] }), source('source-2', { status: 'failed', notice: '相关文章检索超时，热点结果已保留。' }),
  ])] }))
  assert.match(html, /未检索到相关新闻/)
  assert.match(html, /相关文章检索超时，热点结果已保留/)
  assert.ok(!html.includes('相关新闻标题'))
  const historical = await renderToString(vue.createSSRApp(component(), { tools: [trace({ result: { items: [] } })] }))
  assert.ok(!historical.includes('相关新闻检索结果'))
})

test('panel expansion toggles one article and tracks subsequent tool props', () => {
  const tools = vue.reactive({ tools: [searchTrace({ items: [article('one', { excerpt: '文'.repeat(450) })] })] })
  const state = component(false).setup(tools, { expose: () => {} })
  const key = state.articleKey(state.groups.value[0].key, 'one')
  assert.equal(state.expanded.has(key), false)
  state.toggleExcerpt(key)
  assert.equal(state.expanded.has(key), true)
  state.toggleExcerpt(key)
  assert.equal(state.expanded.has(key), false)
  tools.tools = []
  assert.deepEqual(state.groups.value, [])
})

test('chat panel is attached to saved completed turn tools before the tool log and absent from pending SSE', () => {
  assert.match(viewSource, /<template v-if="turn\.status === 'completed'">[\s\S]*?<RelatedArticlesPanel :tools="turn\.tools" \/>[\s\S]*?<\/template>\s*<details v-if="turn\.tools\.length"/)
  const pendingTemplate = viewSource.slice(viewSource.indexOf('<article v-if="pendingVisible && pending"'))
  assert.ok(!pendingTemplate.includes('<RelatedArticlesPanel'))
  assert.ok(!panelSource.includes('v-html'))
})
