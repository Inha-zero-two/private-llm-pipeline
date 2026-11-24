import assert from 'node:assert/strict'
import { test } from 'node:test'
import { elapsedLabel, pipelineElapsed } from '../src/backend/elapsed.ts'
import { createTracker } from '../src/comparison/tracker.ts'
import { panelView, sendState, turnToShow, usedPairsLabel } from '../src/comparison/view.ts'

const item = (patch = {}) => ({
  runId: 1,
  kind: 'local',
  question: 'q',
  answer: null,
  status: 'running',
  usedPairs: 0,
  droppedPairs: 0,
  retryable: false,
  ...patch,
})
const turn = (runId, patch = {}) => ({ id: `t${runId}`, runId, question: 'q', ...patch })
const flush = () => new Promise((resolve) => setTimeout(resolve, 5))

// 걸린 시간

const at = (seconds) => new Date(Date.UTC(2026, 10, 24, 0, 0, seconds)).toISOString()

test('elapsed time excludes time spent waiting for consent', () => {
  const run = { startedAt: at(0), endedAt: at(30) }
  assert.deepEqual(pipelineElapsed({ ...run, consents: [] }), { ms: 30000, consentExcluded: false })
  const consents = [
    { createdAt: at(2), respondedAt: at(10) },
    { createdAt: at(12), respondedAt: at(22) },
  ]
  const elapsed = pipelineElapsed({ ...run, consents })
  assert.deepEqual(elapsed, { ms: 12000, consentExcluded: true })
  assert.equal(elapsedLabel(elapsed), '12.0초 (동의 대기·해석 제외)')
  assert.equal(elapsedLabel({ ms: 12440, consentExcluded: false }), '12.4초')
})

test('elapsed time is hidden without times and never negative', () => {
  assert.equal(pipelineElapsed({}), null)
  assert.equal(pipelineElapsed({ startedAt: at(0), endedAt: null }), null)
  assert.equal(pipelineElapsed({ startedAt: '', endedAt: at(1) }), null)
  assert.equal(pipelineElapsed({ startedAt: 'x', endedAt: 'y' }), null)
  const consents = [{ createdAt: at(0), respondedAt: at(9) }]
  assert.equal(pipelineElapsed({ startedAt: at(0), endedAt: at(5), consents }).ms, 0)
  const waiting = [{ createdAt: at(0), respondedAt: null }]
  assert.deepEqual(pipelineElapsed({ startedAt: at(0), endedAt: at(5), consents: waiting }), {
    ms: 5000,
    consentExcluded: false,
  })
})

// 패널

test('used pairs show only counts', () => {
  const label = (usedPairs, droppedPairs) => usedPairsLabel(item({ usedPairs, droppedPairs }))
  assert.equal(label(0, 0), '앞 대화 없음')
  assert.equal(label(3, 0), '앞 대화 3개 사용')
  assert.equal(label(3, 1), '앞 대화 3개 사용 (길이 때문에 1개 제외)')
  assert.equal(label(0, 4), '앞 대화 0개 사용 (길이 때문에 4개 제외)')
  assert.equal(label(null, 0), null)
})

test('the latest question is shown unless another one is selected', () => {
  const turns = [turn(1), turn(2), turn(undefined, { running: true })]
  assert.equal(turnToShow([], null), null)
  assert.equal(turnToShow(turns, null), turns[2])
  assert.equal(turnToShow(turns, 1), turns[0])
  assert.equal(turnToShow(turns, 99), turns[2])
})

const state = (patch = {}) => ({
  enabled: true,
  running: null,
  chats: {},
  failedChats: {},
  ...patch,
})
const chat = (registered, items) => state({ chats: { 7: { registered, items } } })
const message = (s, t = turn(1)) => panelView(s, '7', t, 'local').message

test('panel messages for chats and questions without a result', () => {
  assert.match(panelView(state(), null, null, 'local').message, /질문을 보내면/)
  assert.match(message(state()), /불러오는 중/)
  assert.match(message(state({ failedChats: { 7: true } })), /불러오지 못했습니다/)
  assert.equal(message(chat(false, [])), '이 채팅은 비교하지 않습니다.')
  const none = '이 질문의 비교 기록이 없습니다.'
  assert.equal(message(chat(true, [])), none)
  assert.equal(message(chat(true, [item({ runId: 2 })])), none)
  assert.equal(message(chat(false, [item({ runId: 2 })])), none)
  assert.match(message(chat(true, []), turn(undefined, { running: true })), /보내는 중/)
})

test('an unregistered chat still shows its saved results', () => {
  const saved = item({ status: 'ok', answer: '답' })
  assert.deepEqual(panelView(chat(false, [saved]), '7', turn(1), 'local'), {
    item: saved,
    banner: '이 채팅은 더 이상 비교하지 않습니다.',
  })
  assert.equal(panelView(chat(true, [saved]), '7', turn(1), 'local').banner, null)
  assert.equal(panelView(chat(true, [saved]), '7', turn(1), 'frontier').item, null)
})

// 전송 막기

test('sending waits for all three outputs in every chat', () => {
  const running = { chatId: 'a', runId: 1 }
  const free = { running: false, comparing: false, blockedReason: null }
  assert.deepEqual(sendState('a', null, null), free)
  assert.deepEqual(sendState(null, null, null), free)
  assert.deepEqual(sendState('a', 'a', running), { ...free, running: true })
  assert.deepEqual(sendState('a', 'a', null), { ...free, running: true })
  // 파이프라인이 끝나고 비교만 남음
  assert.deepEqual(sendState('a', null, running), { ...free, running: true, comparing: true })
  // 다른 채팅에서도 막는다
  assert.match(sendState('b', 'a', running).blockedReason, /다른 채팅에서 답변/)
  assert.match(sendState('b', null, running).blockedReason, /다른 채팅에서 비교 답변/)
  assert.match(sendState(null, null, running).blockedReason, /비교 답변/)
  assert.equal(sendState('b', null, running).running, false)
})

// 실시간 이벤트

class FakeEventSource {
  static CLOSED = 2
  static all = []
  readyState = 1
  listeners = new Map()
  constructor(url) {
    this.url = url
    FakeEventSource.all.push(this)
  }
  addEventListener(name, listener) {
    this.listeners.set(name, listener)
  }
  close() {
    this.readyState = FakeEventSource.CLOSED
  }
  send(name, data = {}) {
    this.listeners.get(name)({ data: JSON.stringify(data) })
  }
}

/** 서버 대역. routes: {"GET /path": 응답 또는 함수}. 없는 주소는 404 */
function setup(t, routes) {
  const names = ['EventSource', 'fetch']
  const previous = names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)])
  t.after(() => {
    for (const [name, descriptor] of previous) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor)
      else delete globalThis[name]
    }
  })
  FakeEventSource.all = []
  const calls = []
  globalThis.EventSource = FakeEventSource
  globalThis.fetch = async (url, { method }) => {
    calls.push(`${method} ${url}`)
    const route = routes[`${method} ${url}`]
    const data = typeof route === 'function' ? await route() : route
    if (data === undefined) {
      const body = JSON.stringify({ code: 'NOT_FOUND', message: '없음' })
      return { ok: false, status: 404, text: async () => body }
    }
    return { ok: true, text: async () => JSON.stringify(data) }
  }
  const tracker = createTracker(1)
  t.after(() => tracker.close())
  return { tracker, calls, latest: () => FakeEventSource.all.at(-1) }
}

const STATUS = 'GET /api/comparisons/status'
const CHAT = 'GET /api/chats/7/comparisons'
const on = { enabled: true, running: null }

test('a question is followed from start to done', async (t) => {
  let saved = { registered: true, items: [] }
  const { tracker, latest } = setup(t, { [STATUS]: on, [CHAT]: () => saved })
  await tracker.sync()
  assert.equal(tracker.get().enabled, true)
  tracker.started('7', 1)
  assert.deepEqual(tracker.get().running, { chatId: '7', runId: 1 })
  assert.equal(latest().url, '/api/runs/1/comparisons/events')
  latest().send('snapshot', { items: [item(), item({ kind: 'frontier', answer: '앞' })] })
  latest().send('token', { kind: 'frontier', text: '뒤' })
  latest().send('token', { kind: 'local', text: '가' })
  const answers = () => tracker.get().chats[7].items.map((i) => i.answer)
  assert.deepEqual(answers(), ['가', '앞뒤'])
  latest().send('result', item({ status: 'failed', answer: '가', retryable: false }))
  assert.equal(tracker.get().chats[7].items[0].status, 'failed')
  saved = { registered: true, items: [item({ status: 'failed', retryable: true })] }
  latest().send('done')
  assert.equal(tracker.get().running, null)
  assert.equal(latest().readyState, FakeEventSource.CLOSED)
  await flush()
  assert.equal(tracker.get().chats[7].items[0].retryable, true)
  assert.equal(FakeEventSource.all.length, 1)
})

test('a reconnected snapshot replaces the text instead of appending', async (t) => {
  const { tracker, latest } = setup(t, { [STATUS]: on, [CHAT]: { registered: true, items: [] } })
  await tracker.sync()
  tracker.started('7', 1)
  latest().send('snapshot', { items: [item({ answer: '가나' })] })
  latest().send('token', { kind: 'local', text: '다' })
  latest().send('snapshot', { items: [item({ answer: '가나다라' })] })
  latest().send('token', { kind: 'local', text: '마' })
  assert.equal(tracker.get().chats[7].items.length, 1)
  assert.equal(tracker.get().chats[7].items[0].answer, '가나다라마')
})

test('a slow list response does not overwrite newer live text', async (t) => {
  let respond
  const slow = new Promise((resolve) => {
    respond = resolve
  })
  const { tracker, latest } = setup(t, { [STATUS]: on, [CHAT]: () => slow })
  await tracker.sync()
  tracker.started('7', 2)
  latest().send('snapshot', { items: [item({ runId: 2, answer: '새 글' })] })
  const old = item({ runId: 1, status: 'ok', answer: '지난 답' })
  respond({ registered: true, items: [old, item({ runId: 2, answer: '옛 글' })] })
  await flush()
  assert.deepEqual(
    tracker.get().chats[7].items.map((i) => i.answer),
    ['지난 답', '새 글'],
  )
})

test('reloading the page restores a running comparison', async (t) => {
  const { tracker, latest } = setup(t, {
    [STATUS]: { enabled: true, running: { chatId: 7, runId: 3 } },
    [CHAT]: { registered: true, items: [] },
  })
  await tracker.sync()
  assert.deepEqual(tracker.get().running, { chatId: '7', runId: 3 })
  assert.equal(latest().url, '/api/runs/3/comparisons/events')
  // 이미 받고 있으면 다시 연결하지 않는다
  await tracker.sync()
  assert.equal(FakeEventSource.all.length, 1)
})

test('a closed connection is recovered or finished', async (t) => {
  let status = { enabled: true, running: { chatId: 7, runId: 1 } }
  const { tracker, latest } = setup(t, {
    [STATUS]: () => status,
    [CHAT]: { registered: true, items: [item({ status: 'ok' })] },
  })
  await tracker.sync()
  latest().readyState = FakeEventSource.CLOSED
  latest().send('error')
  await flush()
  assert.equal(FakeEventSource.all.length, 2) // 아직 진행 중이면 다시 붙는다
  status = { enabled: true, running: null }
  latest().readyState = FakeEventSource.CLOSED
  latest().send('error')
  await flush()
  assert.equal(tracker.get().running, null) // 끝났으면 막은 것을 푼다
  assert.equal(tracker.get().chats[7].items[0].status, 'ok')
  assert.equal(FakeEventSource.all.length, 2)
})

test('a temporary error leaves reconnecting to EventSource', async (t) => {
  const { tracker, calls, latest } = setup(t, {
    [STATUS]: on,
    [CHAT]: { registered: true, items: [] },
  })
  await tracker.sync()
  tracker.started('7', 1)
  await flush()
  const before = calls.length
  latest().send('error')
  await flush()
  assert.equal(calls.length, before)
  assert.notEqual(tracker.get().running, null)
})

test('events of an old connection are ignored', async (t) => {
  const { tracker, latest } = setup(t, { [STATUS]: on, [CHAT]: { registered: true, items: [] } })
  await tracker.sync()
  tracker.started('7', 1)
  const old = latest()
  tracker.started('7', 2)
  old.send('done')
  assert.deepEqual(tracker.get().running, { chatId: '7', runId: 2 })
})

test('when the feature is off nothing is followed', async (t) => {
  const { tracker } = setup(t, { [STATUS]: { enabled: false, running: null } })
  await tracker.sync()
  tracker.started('7', 1)
  await flush()
  assert.equal(tracker.get().enabled, false)
  assert.equal(tracker.get().running, null)
  assert.equal(FakeEventSource.all.length, 0)
})

test('retry follows the run and reports refusal', async (t) => {
  const { tracker, calls, latest } = setup(t, {
    [STATUS]: on,
    [CHAT]: { registered: true, items: [] },
    'POST /api/runs/1/comparisons/local/retry': null,
    'POST /api/runs/1/stop': null,
  })
  await tracker.sync()
  await assert.rejects(() => tracker.retry('7', 2, 'local'), { message: '없음' })
  assert.equal(tracker.get().running, null)
  await tracker.retry('7', 1, 'local')
  assert.deepEqual(tracker.get().running, { chatId: '7', runId: 1 })
  assert.equal(latest().url, '/api/runs/1/comparisons/events')
  tracker.stop()
  await flush()
  assert.equal(calls.at(-1), 'POST /api/runs/1/stop')
})

test('failed loading is reported and cleared by the next success', async (t) => {
  const routes = { [STATUS]: on }
  const { tracker } = setup(t, routes)
  await tracker.load('7')
  assert.equal(tracker.get().failedChats[7], true)
  routes[CHAT] = { registered: false, items: [] }
  await tracker.load('7')
  assert.deepEqual(tracker.get().failedChats, {})
  tracker.forget('7')
  assert.deepEqual(tracker.get().chats, {})
})
