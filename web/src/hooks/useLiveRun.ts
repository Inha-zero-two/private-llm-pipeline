import { useEffect, useReducer, useRef } from 'react'
import { api, type ConsentResultValue, type EndReason, type RunEvent } from '@/api'

type ConsentRequestData = Extract<RunEvent, { type: 'consent_request' }>['data']

export type LiveRun = {
  runId: string
  lastId: number
  step: string | null
  frontier: { round: number; request: string; answer: string | null }[]
  /** 응답을 기다리는 동의 요청 */
  consent: ConsentRequestData | null
  consentResults: { items: ConsentRequestData['items']; result: ConsentResultValue }[]
  answer: string
  done: { endReason: EndReason; limited: string | null } | null
  error: { code: string; message: string } | null
  /** 서버와 SSE 연결이 끊겨 다시 접속하는 중 */
  reconnecting: boolean
}

const initial = (runId: string): LiveRun => ({
  runId,
  lastId: 0,
  step: null,
  frontier: [],
  consent: null,
  consentResults: [],
  answer: '',
  done: null,
  error: null,
  reconnecting: false,
})

type Action =
  | { type: 'reset'; runId: string }
  | { type: 'event'; event: RunEvent }
  | { type: 'connection'; connected: boolean }

function reducer(state: LiveRun, action: Action): LiveRun {
  if (action.type === 'reset') return initial(action.runId)
  if (action.type === 'connection') return { ...state, reconnecting: !action.connected }
  const e = action.event
  // 재접속으로 다시 받은 이벤트는 건너뛴다 (A-01)
  if (e.id <= state.lastId) return state
  const s = { ...state, lastId: e.id }

  switch (e.type) {
    case 'step':
      return { ...s, step: e.data.label }
    case 'frontier':
      return { ...s, frontier: [...s.frontier, e.data] }
    case 'consent_request':
      return { ...s, consent: e.data }
    case 'consent_result':
      return {
        ...s,
        consent: null,
        consentResults: s.consent
          ? [...s.consentResults, { items: s.consent.items, result: e.data.result }]
          : s.consentResults,
      }
    case 'token':
      return { ...s, answer: s.answer + e.data.text }
    case 'done':
      return { ...s, step: null, consent: null, done: e.data }
    case 'error':
      return { ...s, step: null, consent: null, error: e.data }
  }
}

/**
 * 진행 중인 실행의 SSE를 구독해 화면 상태로 모은다.
 * 실행이 끝나거나 스트림이 끊기면 onFinished를 부른다 (채팅 다시 읽기용).
 */
export function useLiveRun(runId: string | null, onFinished: () => void): LiveRun | null {
  const [state, dispatch] = useReducer(reducer, null, () => initial(''))
  const onFinishedRef = useRef(onFinished)

  useEffect(() => {
    onFinishedRef.current = onFinished
  })

  useEffect(() => {
    if (!runId) return
    dispatch({ type: 'reset', runId })
    return api.subscribeRun(runId, {
      onEvent: (event) => {
        dispatch({ type: 'event', event })
        if (event.type === 'done' || event.type === 'error') onFinishedRef.current()
      },
      onClosed: () => onFinishedRef.current(),
      onConnection: (connected) => dispatch({ type: 'connection', connected }),
    })
  }, [runId])

  return runId && state.runId === runId ? state : null
}
