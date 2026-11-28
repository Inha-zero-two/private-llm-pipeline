import type { Consent, MonitorEvent, MonitorState, Query, RunDetail } from './types.ts'

export const emptyMonitor = (): MonitorState => ({
  run: null,
  chatTitle: '',
  question: '',
  step: null,
  events: [],
  connection: 'loading',
  connectionMessage: null,
})

type Data = Record<string, unknown>

/** 저장된 상세를 기준으로 SSE를 재생한다. 성공·실패가 확정된 질의를 대기로 되돌리지 않는다. */
export function applyEvent(
  state: MonitorState,
  type: string,
  data: Data,
  id: number,
): MonitorState {
  if (
    !state.run ||
    type === 'token' ||
    type === 'frontier' ||
    state.events.some((e) => e.id === id)
  )
    return state
  let run = { ...state.run }
  let step = state.step
  const at = typeof data.createdAt === 'string' ? data.createdAt : null
  let text: string
  let source: MonitorEvent['source'] = 'local'
  let round: number | undefined
  switch (type) {
    case 'step':
      step = String(data.label ?? '')
      text = step
      source = data.stage === 'FRONTIER' ? 'external' : 'local'
      break
    case 'frontier_request': {
      round = Number(data.round)
      source = 'external'
      text = '외부 질의 시작'
      const query: Query = {
        queryId: Number(data.queryId),
        round,
        request: String(data.request ?? ''),
        answer: null,
        status: 'uncertain',
        createdAt: at ?? undefined,
      }
      if (!run.frontierQueries.some((q) => q.queryId === query.queryId))
        run.frontierQueries = [...run.frontierQueries, query].sort((a, b) => a.round - b.round)
      break
    }
    case 'frontier_result': {
      round = Number(data.round)
      source = 'external'
      const status = data.status === 'ok' ? 'ok' : 'failed'
      text = status === 'ok' ? '외부 응답 수신' : '외부 질의 실패'
      run.frontierQueries = run.frontierQueries.map((q) =>
        q.queryId === data.queryId
          ? { ...q, status, answer: typeof data.answer === 'string' ? data.answer : null }
          : q,
      )
      break
    }
    case 'consent_request': {
      text = '공개 동의 요청'
      const consent: Consent = {
        consentId: Number(data.consentId),
        items: (data.items ?? []) as Consent['items'],
        result: null,
        responseText: null,
        decisions: [],
        purpose: String(data.purpose ?? ''),
        createdAt: at ?? undefined,
      }
      if (!run.consents.some((c) => c.consentId === consent.consentId))
        run.consents = [...run.consents, consent]
      break
    }
    case 'consent_result':
      text = data.result === 'cancelled' ? '공개 동의 요청 취소' : '공개 동의 응답 기록'
      run.consents = run.consents.map((c) =>
        c.consentId === data.consentId
          ? {
              ...c,
              result: data.result === 'cancelled' ? 'cancelled' : 'answered',
              responseText: typeof data.text === 'string' ? data.text : null,
              decisions: (data.decisions ?? []) as Consent['decisions'],
              respondedAt: at,
            }
          : c,
      )
      break
    case 'done':
      text = '실행 종료'
      run = {
        ...run,
        status: 'finished',
        endReason: String(data.endReason ?? ''),
        notice: typeof data.limited === 'string' ? data.limited : null,
        endedAt: run.endedAt ?? at,
      }
      break
    case 'error':
      text = String(data.message ?? '실행 오류')
      run = {
        ...run,
        status: 'finished',
        endReason: null,
        errorCode: String(data.code ?? 'INTERNAL_ERROR'),
        errorStage: typeof data.stage === 'string' ? data.stage : null,
        notice: text,
        endedAt: run.endedAt ?? at,
      }
      break
    default:
      return state
  }
  return {
    ...state,
    run,
    step,
    events: [...state.events, { id, type, text, source, round, createdAt: at }],
  }
}

export function mergeDetail(state: MonitorState, run: RunDetail): MonitorState {
  // 조회 시점보다 뒤에 온 SSE를 덮어쓰지 않는다. 종료된 상세는 최종 저장 상태로 확정한다.
  if (run.status === 'finished') return { ...state, run }
  return {
    ...state,
    run: state.run
      ? {
          ...run,
          ...state.run,
          startedAt: run.startedAt,
          frontierQueries: run.frontierQueries.reduce<Query[]>((queries, q) => {
            const live = queries.find((item) => item.queryId === q.queryId)
            return live
              ? queries.map((item) =>
                  item.queryId === q.queryId && item.status === 'uncertain' ? q : item,
                )
              : [...queries, q]
          }, state.run.frontierQueries),
        }
      : run,
  }
}

export function selectAfterArrival(selected: number, previousCount: number, count: number): number {
  if (!count) return 0
  if (count > previousCount && selected >= previousCount - 1) return count - 1
  return Math.min(selected, count - 1)
}

export function eventElapsed(startedAt: string | undefined, createdAt: string | null): string {
  const ms = Date.parse(createdAt ?? '') - Date.parse(startedAt ?? '')
  if (!Number.isFinite(ms)) return '—'
  return `${(Math.max(0, ms) / 1000).toFixed(1)}s`
}
