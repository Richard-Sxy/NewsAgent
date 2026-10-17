import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

function load(source) {
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  })
  const exports = {}
  new Function('require', 'exports', outputText)(assertFn, exports)
  return exports
}

function assertFn(name) {
  assert.ok(name === undefined, `Unexpected runtime import: ${name}`)
}

const { turnDurationSeconds, formatTurnDuration } = load(
  readFileSync(new URL('../src/utils/turnDuration.ts', import.meta.url), 'utf8'),
)
const viewSource = readFileSync(new URL('../src/views/ChatView.vue', import.meta.url), 'utf8')

const turn = (overrides = {}) => ({
  status: 'completed',
  created_at: '2026-10-12T08:00:00.000Z',
  completed_at: '2026-10-12T08:00:03.460Z',
  ...overrides,
})

test('terminal turn duration is derived from persisted timestamps', () => {
  assert.equal(turnDurationSeconds(turn()), 3.46)
  assert.equal(turnDurationSeconds(turn({ status: 'failed' })), 3.46)
})

test('timezone offsets are normalized before subtracting', () => {
  assert.equal(turnDurationSeconds(turn({
    created_at: '2026-10-12T16:00:00.000+08:00',
    completed_at: '2026-10-12T08:00:05.000Z',
  })), 5)
})

test('processing or incomplete turns expose no duration', () => {
  assert.equal(turnDurationSeconds(turn({ status: 'processing' })), null)
  assert.equal(turnDurationSeconds(turn({ completed_at: null })), null)
  assert.equal(turnDurationSeconds(turn({ created_at: null })), null)
})

test('malformed or inverted timestamps are rejected', () => {
  assert.equal(turnDurationSeconds(turn({ completed_at: '2026-10-12 08:00:03' })), null)
  assert.equal(turnDurationSeconds(turn({ created_at: 'not-a-time' })), null)
  assert.equal(
    turnDurationSeconds(turn({ completed_at: '2026-10-12T07:59:59.000Z' })),
    null,
  )
})

test('duration renders with exactly one decimal place', () => {
  assert.equal(formatTurnDuration(3.46), '3.5')
  assert.equal(formatTurnDuration(12.34), '12.3')
  assert.equal(formatTurnDuration(0.04), '0.0')
  assert.equal(formatTurnDuration(105), '105.0')
  assert.equal(formatTurnDuration(turnDurationSeconds(turn())), '3.5')
})

test('invalid durations never render', () => {
  assert.equal(formatTurnDuration(null), null)
  assert.equal(formatTurnDuration(-0.1), null)
  assert.equal(formatTurnDuration(Number.NaN), null)
  assert.equal(formatTurnDuration(Number.POSITIVE_INFINITY), null)
})

test('chat view renders the duration footer inside the assistant bubble', () => {
  assert.match(viewSource, /turnDuration\(turn\)/)
  assert.match(viewSource, /全链路耗时 /)
  assert.match(viewSource, /na-chat__duration/)
  assert.ok(
    viewSource.indexOf('na-chat__duration') > viewSource.indexOf('na-chat__audit'),
    '耗时统计应位于助手消息气泡的最下方',
  )
})
