// 화면 ↔ 앱 API 자료형
// 기준: 상세 설계서 7장 + API_보완_제안서.md (A-01 ~ A-08, 💡 팀 확인 전)

export type EndReason =
  | 'info_enough'
  | 'call_limit'
  | 'regen_limit'
  | 'budget_limit'
  | 'offline'
  | 'stopped'
  | 'consent_denied'

/** 0: 식별정보, 1: 민감정보, 2: 속성정보 */
export type PDCategory = 0 | 1 | 2

export type ChatSummary = {
  chatId: string
  title: string
  activeRunId: string | null // A-06
}

export type Message = {
  messageId: string
  role: 'user' | 'assistant'
  content: string
  createdAt: string
}

export type FrontierQuery = {
  queryId: string
  round: number
  request: string
  answer: string | null
  status: 'ok' | 'failed' | 'uncertain'
}

export type Run = {
  runId: string
  userMessageId: string
  answerMessageId: string | null
  status: 'running' | 'finished'
  endReason: EndReason | null
  limited: string | null
  frontierQueries: FrontierQuery[]
}

// A-02
export type ChatDetail = {
  chatId: string
  title: string
  activeRunId: string | null
  messages: Message[]
  runs: Run[]
}

export type Rule = {
  target: string
  scope: 'item' | 'category'
  action: 'allow' | 'deny'
}

// A-04
export type PrivacySetting = {
  settingId: number | null
  text: string
  rules: Rule[]
  updatedAt: string | null
}

export type SaveResult = {
  ok: boolean
  rules: Rule[]
  message: string
}

export type ConsentItem = {
  pdId: number
  text: string
  category: PDCategory
}

export type ConsentResultValue = 'allow' | 'deny' | 'cancelled'

// SSE 이벤트 (7.2 + A-03, A-05, A-08). id는 실행 안에서 1부터 증가 (A-01)
export type RunEvent = { id: number } & (
  | { type: 'step'; data: { label: string } }
  | { type: 'frontier'; data: { round: number; request: string; answer: string | null } }
  | {
      type: 'consent_request'
      data: { items: ConsentItem[]; purpose: string; benefit: string; risk: string }
    }
  | { type: 'consent_result'; data: { result: ConsentResultValue } }
  | { type: 'token'; data: { text: string } }
  | {
      type: 'done'
      data: { endReason: EndReason; limited: string | null; answerMessageId: string | null }
    }
  | { type: 'error'; data: { code: string; message: string } }
)

export type RunEventType = RunEvent['type']

// A-11. 웹과 서버가 따로 있으므로 화면이 서버 · 로컬 모델 상태를 알아야 한다
export type Health = {
  localLLM: 'ready' | 'loading' | 'down'
  frontier: 'online' | 'offline'
}

export type RunSubscription = {
  onEvent: (event: RunEvent) => void
  /** 스트림이 done/error 없이 끊겼을 때 (예: 404, 서버 종료) */
  onClosed: () => void
  /** 연결이 끊겨 다시 접속하는 중이면 false, 다시 붙으면 true */
  onConnection?: (connected: boolean) => void
}

export interface Api {
  getHealth(): Promise<Health>
  listChats(): Promise<ChatSummary[]>
  createChat(): Promise<{ chatId: string }>
  getChat(chatId: string): Promise<ChatDetail>
  deleteChat(chatId: string): Promise<void>
  sendMessage(chatId: string, text: string): Promise<{ runId: string }>
  /** 반환값을 호출하면 구독을 끊는다 */
  subscribeRun(runId: string, sub: RunSubscription): () => void
  stopRun(runId: string): Promise<void>
  answerConsent(runId: string, allow: boolean): Promise<void>
  getPrivacySetting(): Promise<PrivacySetting>
  savePrivacySetting(text: string): Promise<SaveResult>
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string

  constructor(status: number, code: string, message: string) {
    super(message)
    this.status = status
    this.code = code
  }
}
