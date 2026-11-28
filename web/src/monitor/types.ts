import type { ConsentRequest, Turn } from '../types'

export type MonitorTarget =
  { kind: 'remote'; chatId: string; runId: number } | { kind: 'demo'; chatId: string; id: string }

export type Query = {
  queryId: number
  round: number
  request: string
  answer: string | null
  status: 'ok' | 'failed' | 'uncertain'
  createdAt?: string
}

export type Item = { pdId: number; label: string; text: string; category: number }
export type Consent = {
  consentId: number
  items: Item[]
  result: 'answered' | 'cancelled' | null
  responseText: string | null
  decisions: { pdId: number; allow: boolean }[]
  purpose?: string
  createdAt?: string
  respondedAt?: string | null
}

export type RunDetail = {
  runId: number
  chatId: number
  userMessageId: number
  status: 'running' | 'finished'
  endReason: string | null
  notice: string | null
  errorCode: string | null
  errorStage: string | null
  startedAt: string
  endedAt: string | null
  frontierQueries: Query[]
  consents: Consent[]
}

export type MonitorEvent = {
  id: number
  type: string
  createdAt: string | null
  source: 'local' | 'external'
  text: string
  round?: number
}

export type MonitorState = {
  run: RunDetail | null
  chatTitle: string
  question: string
  step: string | null
  events: MonitorEvent[]
  /** 연결 오류는 실행 오류와 분리한다 */
  connection: 'loading' | 'connected' | 'reconnecting' | 'finished' | 'missing'
  connectionMessage: string | null
  /** 서버 코드가 없는 브라우저 데모 오류를 임의 코드 없이 표시한다 */
  demoError?: boolean
}

export type DemoSnapshot = {
  target: Extract<MonitorTarget, { kind: 'demo' }>
  chatTitle: string
  turn: Turn
  consent: ConsentRequest | null
  startedAt: string
}
