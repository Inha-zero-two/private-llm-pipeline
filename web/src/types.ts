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
  text: string
  category: PDCategory
}

/** 추가 개인정보 공개 요청 */
export type ConsentRequest = {
  items: ConsentItem[]
  purpose: string
  benefit: string
  risk: string
}

export type ConsentResult = 'allow' | 'deny' | 'cancelled'

export type FrontierQuery = {
  round: number
  request: string
  answer: string
}

export type Notice = { kind: 'limited' | 'stopped' | 'error'; text: string }

/** 질문 하나와 그에 대한 답변 */
export type Turn = {
  id: string
  question: string
  running: boolean
  /** 진행 중인 단계 이름 */
  step: string | null
  frontier: FrontierQuery[]
  consentResults: { items: ConsentItem[]; result: ConsentResult }[]
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
