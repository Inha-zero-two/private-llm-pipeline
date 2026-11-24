// 비교 기능(로컬 단독 · 프론티어 단독)에서 쓰는 자료형

export type ComparisonKind = 'local' | 'frontier'

export type ComparisonStatus = 'running' | 'ok' | 'truncated' | 'failed' | 'stopped'

/** 모델 하나의 비교 결과 (서버 응답 그대로) */
export type ComparisonItem = {
  runId: number
  kind: ComparisonKind
  question: string
  answer: string | null
  status: ComparisonStatus
  errorCode: string | null
  message: string | null
  modelName: string | null
  attempts: number
  /** 실제로 보낸 앞 질문·답변 쌍의 수. 모델에 보내지 못했으면 null */
  usedPairs: number | null
  droppedPairs: number
  inputTokens: number | null
  outputTokens: number | null
  cost: number | null
  elapsedMs: number | null
  createdAt: string
  endedAt: string | null
  retryable: boolean
}

export type ChatComparisons = {
  /** 비교 대상 채팅인지 */
  registered: boolean
  items: ComparisonItem[]
}

export type RunningComparison = { chatId: string; runId: number }

export type ComparisonState = {
  /** 서버에서 비교 기능이 켜져 있는지. 확인하기 전에는 false */
  enabled: boolean
  running: RunningComparison | null
  /** 채팅별 결과. 불러오기 전에는 없다 */
  chats: Record<string, ChatComparisons>
  /** 결과를 불러오지 못한 채팅 */
  failedChats: Record<string, true>
}
