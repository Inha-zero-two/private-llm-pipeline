// 목업 실행 시나리오. 질문에 아래 낱말을 넣으면 해당 상황을 볼 수 있다.
//   "오류"     → error 이벤트로 끝남
//   "오프라인" → 외부 모델 연결 불가 (offline)
//   "상한"     → 질문 상한 도달 (call_limit)
//   나이("27살") · 전화번호 · 회사/학교 이름("삼성전자", "인하대학교") → 추가 공개 동의 요청
//   "동의"     → 위 항목이 없어도 예시 항목으로 동의 요청

import type { ConsentItem, EndReason, PDCategory, Rule } from '../types'

// 상세 설계서 3.4 EndReason 표의 화면 안내
export const LIMITED_TEXT: Record<EndReason, string | null> = {
  info_enough: null,
  call_limit: '질문 횟수 한도에 도달해 지금까지 모은 정보로 답했습니다.',
  regen_limit: '개인정보가 포함되지 않은 질문을 만들지 못해 외부 모델에 묻지 못했습니다.',
  budget_limit: '사용 비용 한도에 도달해 지금까지 모은 정보로 답했습니다.',
  offline: '외부 모델에 연결할 수 없어 현재까지 취합한 정보를 바탕으로 로컬 모델로 답했습니다.',
  stopped: '중지되었습니다.',
  consent_denied: '공개 동의가 필요한 정보는 보내지 않고 지금까지 모은 정보로 답했습니다.',
}

type Detector = { pattern: RegExp; category: PDCategory; label: string }

const DETECTORS: Detector[] = [
  { pattern: /01[016789]-?\d{3,4}-?\d{4}/g, category: 0, label: '전화번호' },
  { pattern: /\d{1,3}\s*살/g, category: 2, label: '나이' },
  { pattern: /[가-힣A-Za-z]+(?:전자|그룹|은행|대학교)/g, category: 2, label: '직장' },
]

type DetectedItem = ConsentItem & { label: string }

export type Scenario = {
  kind: 'normal' | 'error' | 'offline' | 'call_limit'
  rounds: number
  consentItems: ConsentItem[]
  frontierRequest: (round: number, consented: boolean) => string
  frontierAnswer: (round: number) => string
  finalAnswer: string
}

function detect(question: string): DetectedItem[] {
  const items: DetectedItem[] = []
  for (const d of DETECTORS) {
    for (const m of question.matchAll(d.pattern)) {
      items.push({ pdId: items.length + 1, text: m[0], category: d.category, label: d.label })
    }
  }
  if (items.length === 0 && question.includes('동의')) {
    items.push(
      { pdId: 1, text: '직무: 개발자', category: 2, label: '직무' },
      { pdId: 2, text: '경력 기간: 3년', category: 2, label: '경력' },
    )
  }
  return items
}

// 설정 규칙에서 허용된 항목은 동의를 묻지 않는다. (목업은 대상 이름이 종류 이름에 들어가는지만 본다)
function isAllowed(item: DetectedItem, rules: Rule[]) {
  const matched = rules.filter(
    (r) => item.label.includes(r.target) || r.target.includes(item.label),
  )
  return matched.length > 0 && matched.every((r) => r.action === 'allow')
}

function mask(question: string, items: DetectedItem[]) {
  return items.reduce((q, item) => q.replaceAll(item.text, `[${item.label}]`), question)
}

export function buildScenario(question: string, rules: Rule[]): Scenario {
  const kind: Scenario['kind'] = question.includes('오류')
    ? 'error'
    : question.includes('오프라인')
      ? 'offline'
      : question.includes('상한')
        ? 'call_limit'
        : 'normal'

  const detected = detect(question)
  const needConsent = detected.filter((item) => !isAllowed(item, rules))
  const masked = mask(question, needConsent)

  return {
    kind,
    rounds: kind === 'call_limit' ? 3 : 2,
    consentItems: needConsent.map(({ label: _label, ...item }) => item),
    frontierRequest: (round, consented) => {
      const q = consented ? question : masked
      return round === 1
        ? `다음 요청에 답하는 데 필요한 일반적인 정보를 알려 주세요. 개인을 특정할 수 있는 내용은 빼고 답해 주세요.\n\n요청: ${q}`
        : `앞의 답변에서 실제로 따라 할 수 있는 단계와 주의할 점을 더 구체적으로 알려 주세요.`
    },
    frontierAnswer: (round) =>
      round === 1
        ? '이런 요청에는 보통 목표를 먼저 정하고, 현재 상황을 정리한 뒤, 우선순위에 따라 계획을 세우는 방식이 효과적입니다.'
        : '단계별로는 (1) 현재 상태 점검 (2) 짧은 주기의 목표 설정 (3) 결과 확인과 수정을 반복하는 것이 좋습니다. 무리한 일정은 피하세요.',
    finalAnswer: [
      '질문하신 내용을 바탕으로 정리해 드릴게요.',
      '',
      '## 핵심 정리',
      '',
      '1. **목표 정하기** — 무엇을 이루고 싶은지 한 문장으로 적어 보세요.',
      '2. **현재 상황 점검** — 지금 가진 자원과 제약을 정리합니다.',
      '3. **계획 세우기** — 우선순위가 높은 것부터 짧은 주기로 나눕니다.',
      '',
      '## 다음 단계',
      '',
      '- 이번 주에 할 수 있는 일 한 가지를 정해 바로 시작해 보세요.',
      '- 진행 상황을 기록해 두면 계획을 고치기 쉽습니다.',
      '',
      '> 외부 모델에는 개인정보를 뺀 일반적인 질문만 보냈고, 개인 상황은 이 PC의 로컬 모델이 반영했습니다.',
    ].join('\n'),
  }
}
