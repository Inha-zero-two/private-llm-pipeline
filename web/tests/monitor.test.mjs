import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  applyEvent,
  emptyMonitor,
  mergeDetail,
  selectAfterArrival,
  eventElapsed,
} from '../src/monitor/model.ts'
import { parseTarget } from '../src/monitor/bridge.ts'
import { followMonitor } from '../src/monitor/client.ts'

const run = (patch = {}) => ({
  runId: 7,
  chatId: 1,
  userMessageId: 2,
  status: 'running',
  endReason: null,
  notice: null,
  errorCode: null,
  errorStage: null,
  startedAt: '2026-11-26T00:00:00Z',
  endedAt: null,
  frontierQueries: [],
  consents: [],
  ...patch,
})
const state = (patch = {}) => ({ ...emptyMonitor(), run: run(), ...patch })
const request = { queryId: 10, round: 1, request: '검사된 요청', createdAt: '2026-11-26T00:00:02Z' }

test('monitor shows the request before its response, then retains it on failure', () => {
  let value = applyEvent(state(), 'frontier_request', request, 2)
  assert.equal(value.run.frontierQueries[0].request, request.request)
  assert.equal(value.run.frontierQueries[0].status, 'uncertain')
  value = applyEvent(
    value,
    'frontier_result',
    { queryId: 10, round: 1, status: 'failed', answer: null },
    3,
  )
  assert.equal(value.run.frontierQueries[0].status, 'failed')
  assert.equal(value.run.frontierQueries[0].request, request.request)
  assert.equal(value.events.length, 2)
  assert.deepEqual(applyEvent(value, 'frontier_request', request, 2), value)
})

test('history replay never downgrades a saved successful query to pending', () => {
  const query = { ...request, answer: '저장된 응답', status: 'ok' }
  const value = applyEvent(
    state({ run: run({ frontierQueries: [query] }) }),
    'frontier_request',
    request,
    1,
  )
  assert.equal(value.run.frontierQueries[0].status, 'ok')
  assert.equal(value.run.frontierQueries.length, 1)
  const merged = mergeDetail(
    value,
    run({ frontierQueries: [{ ...query, status: 'uncertain', answer: null }] }),
  )
  assert.equal(merged.run.frontierQueries[0].answer, query.answer)
})

test('consent replay keeps saved decisions and cancellation includes the original items', () => {
  const consent = {
    consentId: 3,
    items: [{ pdId: 1, label: '이름', text: '예시', category: 0 }],
    result: 'answered',
    responseText: '거절',
    decisions: [{ pdId: 1, allow: false }],
  }
  let value = applyEvent(
    state({ run: run({ consents: [consent] }) }),
    'consent_request',
    consent,
    1,
  )
  assert.equal(value.run.consents[0].result, 'answered')
  value = applyEvent(value, 'consent_result', { consentId: 3, result: 'cancelled' }, 2)
  assert.equal(value.run.consents[0].result, 'cancelled')
  assert.deepEqual(value.run.consents[0].items, consent.items)
})

test('latest round follows arrivals but earlier rounds stay selected', () => {
  assert.equal(selectAfterArrival(0, 0, 1), 0)
  assert.equal(selectAfterArrival(0, 1, 2), 1)
  assert.equal(selectAfterArrival(0, 2, 3), 0)
  assert.equal(selectAfterArrival(1, 2, 3), 2)
  assert.equal(selectAfterArrival(2, 3, 0), 0)
  assert.equal(eventElapsed('2026-11-26T00:00:00Z', request.createdAt), '2.0s')
  assert.equal(eventElapsed(undefined, request.createdAt), '—')
})

test('only previously supplied valid run IDs can become a remote monitor target', () => {
  assert.equal(parseTarget(null), null)
  assert.equal(parseTarget('{broken'), null)
  assert.equal(parseTarget(JSON.stringify({ kind: 'remote', chatId: '1', runId: -2 })), null)
  assert.deepEqual(parseTarget(JSON.stringify({ kind: 'remote', chatId: '1', runId: 7 })), {
    kind: 'remote',
    chatId: '1',
    runId: 7,
  })
})

class FakeSource {
  static CLOSED = 2
  static latest
  readyState = 1
  listeners = new Map()
  constructor(url) {
    this.url = url
    FakeSource.latest = this
  }
  addEventListener(name, fn) {
    this.listeners.set(name, fn)
  }
  close() {
    this.readyState = 2
  }
  send(name, data, id) {
    const event = new MessageEvent(name, { data: JSON.stringify(data), lastEventId: String(id) })
    this.listeners.get(name)?.(event)
  }
}

test('separate monitor loads only its known run, distinguishes transport failure, and disposes without stopping it', async (t) => {
  const previousSource = globalThis.EventSource
  const previousFetch = globalThis.fetch
  const calls = []
  globalThis.EventSource = FakeSource
  globalThis.fetch = async (url, options) => {
    calls.push([url, options])
    return {
      ok: true,
      json: async () =>
        url.includes('/chats/')
          ? { title: 'chat A', messages: [{ messageId: 2, content: 'question A' }] }
          : run(),
    }
  }
  t.after(() => {
    globalThis.EventSource = previousSource
    globalThis.fetch = previousFetch
  })
  const received = []
  const dispose = followMonitor({ kind: 'remote', chatId: '1', runId: 7 }, (value) =>
    received.push(value),
  )
  t.after(dispose)
  await new Promise((resolve) => setImmediate(resolve))
  const source = FakeSource.latest
  assert.equal(source.url, '/api/runs/7/events')
  source.onopen()
  source.send('frontier_request', request, 2)
  source.send('frontier_request', request, 2)
  assert.equal(received.at(-1).events.length, 1)
  source.onerror(new Event('error'))
  assert.equal(received.at(-1).connection, 'reconnecting')
  assert.equal(received.at(-1).run.status, 'running')
  source.send('error', { code: 'FRONTIER_TIMEOUT', stage: 'FRONTIER', message: '실행 오류' }, 4)
  assert.equal(received.at(-1).connection, 'finished')
  assert.equal(received.at(-1).run.errorCode, 'FRONTIER_TIMEOUT')
  dispose()
  assert.equal(source.readyState, 2)
  assert(calls.every(([url]) => !url.endsWith('/stop') && !url.endsWith('/messages')))
  const count = received.length
  source.send('step', { label: 'old run' }, 5)
  assert.equal(received.length, count)
})
