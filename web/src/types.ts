// 화면에서 쓰는 자료형

/** 0: 식별정보, 1: 민감정보, 2: 속성정보 */
export type PDCategory = 0 | 1 | 2

export const CATEGORY_LABEL: Record<PDCategory, string> = {
  0: '식별정보',
  1: '민감정보',
  2: '속성정보',
}

export type ConsentItem = {
  id: number
  /** 정보 종류. 예: 나이 */
  label: string
  /** 질문에 나온 값. 예: 27살 */
  text: string
  category: PDCategory
}

/** 추가 개인정보 공개 요청 */
export type ConsentRequest = {
  /** 서버의 동의 요청 ID (데모에는 없음) */
  consentId?: number
  items: ConsentItem[]
  purpose: string
  benefit: string
  risk: string
}

export type ConsentDecision = { item: ConsentItem; allow: boolean }

/** 동의 요청에 대한 응답. 사용자는 자연어로 답하고, 로컬 모델이 항목별 허용 여부로 해석한다 */
export type ConsentRecord =
  | { kind: 'answered'; answer: string; decisions: ConsentDecision[] }
  | { kind: 'cancelled'; items: ConsentItem[] }

export type FrontierQuery = {
  round: number
  request: string
  answer: string
}

export type Notice = { kind: 'limited' | 'stopped' | 'error'; text: string }

/** 질문 하나와 그에 대한 답변 */
export type Turn = {
  id: string
  /** 서버의 실행 ID. 질문을 보낸 뒤 채워진다 (데모에는 없음) */
  runId?: number
  question: string
  running: boolean
  /** 진행 중인 단계 이름 */
  step: string | null
  frontier: FrontierQuery[]
  consents: ConsentRecord[]
  answer: string
  notice: Notice | null
}

export type Chat = {
  id: string
  title: string
  turns: Turn[]
}

export type Rule = {
  target: string
  scope: 'item' | 'category'
  action: 'allow' | 'deny'
}

/** 개인정보 공개 허용 설정 */
export type PrivacySetting = {
  text: string
  rules: Rule[]
}
