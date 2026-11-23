import assert from 'node:assert/strict'
import { test } from 'node:test'
import { monitorTurn } from '../src/components/monitoring.ts'
import { remoteBackend } from '../src/backend/remote.ts'

const turn = (patch = {}) => ({
  id: 'turn',
  question: 'question',
  running: false,
  step: null,
  frontier: [],
  consents: [],
  answer: '',
  notice: null,
  endReason: null,
  ...patch,
})

const reasons = [
  ['info_enough', '완료', '정보 충분'],
  ['call_limit', '제한 종료', '외부 질의 횟수 한도 도달'],
  ['regen_limit', '제한 종료', '요청문 재생성 한도 도달'],
  ['budget_limit', '제한 종료', '비용 한도 초과'],
  ['offline', '외부 연결 불가', '외부 모델 연결 불가'],
  ['stopped', '중지됨', '사용자 중지'],
]
for (const [code, status, label] of reasons) {
  test(`monitoring labels server reason ${code}`, () => {
    const log = monitorTurn(turn({ endReason: code }))
    assert.equal(log.status, status)
    assert.equal(log.endReason, label)
  })
}

test('missing, unknown, legacy and demo reasons stay distinct', () => {
  assert.equal(monitorTurn(turn()).endReason, '종료 사유 미제공')
  assert.equal(monitorTurn(turn(), false, true).endReason, '데모 실행 완료')
  assert.equal(
    monitorTurn(turn({ endReason: 'future' })).endReason,
    '알 수 없는 종료 사유 (future)',
  )
  assert.equal(monitorTurn(turn({ endReason: 'toString' })).status, '종료')
  assert.equal(
    monitorTurn(turn({ endReason: 'consent_denied' })).endReason,
    '공개 거부로 종료 (과거 기록)',
  )
  assert.equal(monitorTurn(turn({ notice: { kind: 'error', text: 'failure' } })).status, '오류')
  assert.equal(monitorTurn(turn({ running: true }), true).endReason, null)
})

class FakeMessageEvent {
  constructor(data) {
    this.data = JSON.stringify(data)
    this.lastEventId = '1'
  }
}
class FakeEventSource {
  static CLOSED = 2
  static latest
  listeners = new Map()
  readyState = 1
  constructor() {
    FakeEventSource.latest = this
  }
  addEventListener(name, listener) {
    this.listeners.set(name, listener)
  }
  close() {
    this.readyState = FakeEventSource.CLOSED
  }
  dispatch(name, data) {
    this.listeners.get(name)(new FakeMessageEvent(data))
  }
}
function remoteWith(t, run) {
  const previous = Object.fromEntries(
    ['EventSource', 'MessageEvent', 'fetch'].map((key) => [
      key,
      Object.getOwnPropertyDescriptor(globalThis, key),
    ]),
  )
  t.after(() => {
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
  })
  Object.assign(globalThis, {
    EventSource: FakeEventSource,
    MessageEvent: FakeMessageEvent,
    fetch: async (url) => ({
      ok: true,
      text: async () =>
        JSON.stringify(
          url.includes('/chats/')
            ? {
                chatId: 1,
                title: 'chat',
                messages: [{ messageId: 1, role: 'user', content: 'question' }],
                runs: [{ runId: 2, userMessageId: 1, status: 'finished', notice: run.notice }],
              }
            : run,
        ),
    }),
  })
  return remoteBackend
}

for (const [code] of reasons) {
  test(`live completion and saved run restore the same reason: ${code}`, async (t) => {
    const notice = code === 'info_enough' ? null : 'detailed notice'
    const run = {
      runId: 2,
      status: 'finished',
      endReason: code,
      notice,
      errorCode: null,
      frontierQueries: [],
      consents: [],
    }
    const remote = remoteWith(t, run)
    let current = turn({ running: true })
    remote.attachRun(2, {
      update: (fn) => {
        current = fn(current)
      },
      onConsent() {},
      onFinish() {},
    })
    FakeEventSource.latest.dispatch('done', { endReason: code, limited: notice })
    const [saved] = await remote.loadTurns('1')
    assert.equal(current.endReason, code)
    assert.equal(saved.endReason, code)
    assert.equal(current.notice?.text ?? null, notice)
    assert.deepEqual(monitorTurn(current), monitorTurn(saved))
  })
}

test('error completion clears any end reason', (t) => {
  const remote = remoteWith(t, {})
  let current = turn({ running: true, endReason: 'offline' })
  remote.attachRun(2, {
    update: (fn) => {
      current = fn(current)
    },
    onConsent() {},
    onFinish() {},
  })
  FakeEventSource.latest.dispatch('error', { code: 'INTERNAL_ERROR', message: 'failure' })
  assert.equal(current.endReason, null)
  assert.equal(monitorTurn(current).endReason, '오류 종료')
})
