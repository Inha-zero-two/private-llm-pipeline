// 공개 동의 답변 해석 데모. 실제로는 로컬 모델이 자연어 답변을 항목별 허용 여부로 바꾼다.
//   "나이는 공개하고 직장은 공개하지 마" → 나이 공개, 직장 비공개
//   "모두 공개해도 돼" / "다 빼줘"        → 전체 공개 / 전체 비공개
//   언급하지 않은 항목은 공개하지 않는다 (기본 금지). "나머지는 공개해"처럼 따로 정할 수 있다.

import { josa } from '@/lib/josa'
import type { ConsentDecision, ConsentItem } from '@/types'

export type ConsentInterpretation =
  { ok: true; decisions: ConsentDecision[] } | { ok: false; message: string }

const DENY =
  /(하지\s*마|말아|말고|숨겨|빼|제외|비공개|안\s*돼|안\s*할|싫|금지|보내지\s*마|공개\s*안|하지\s*않|아니)/
const ALLOW = /(공개|괜찮|보내|허용|돼|좋아|해도|응|네)/

// 정보 종류를 부르는 다른 말
const ALIASES: Record<string, string[]> = {
  직장: ['회사', '학교', '소속'],
  전화번호: ['번호', '연락처'],
}

const REST = '나머지'

/** 공개면 true, 비공개면 false, 판단할 말이 없으면 null */
function verdict(text: string) {
  if (DENY.test(text)) return false
  if (ALLOW.test(text)) return true
  return null
}

function mentionAt(text: string, item: ConsentItem) {
  const words = [item.label, item.text, ...(ALIASES[item.label] ?? [])]
  const found = words.map((w) => text.indexOf(w)).filter((i) => i >= 0)
  return found.length > 0 ? Math.min(...found) : -1
}

export async function interpretConsent(
  answer: string,
  items: ConsentItem[],
): Promise<ConsentInterpretation> {
  await new Promise((r) => setTimeout(r, 500)) // 로컬 모델이 답변을 해석하는 시간
  const text = answer.trim()
  const fail = {
    ok: false,
    message: `어떤 정보를 공개할지 알아듣지 못했어요. "${example(items)}"처럼 답해 주세요.`,
  } as const

  const marks = new Map(items.map((item) => [item.id, mentionAt(text, item)]))
  const restAt = text.indexOf(REST)
  const points = [...marks.values(), restAt].filter((i) => i >= 0).sort((a, b) => a - b)

  // 아무 항목도 짚지 않았으면 답변 전체가 모든 항목에 대한 답이다 ("모두 공개해도 돼")
  if (points.length === 0) {
    const v = verdict(text)
    return v === null ? fail : { ok: true, decisions: items.map((item) => ({ item, allow: v })) }
  }

  // 언급한 위치부터 다음 언급 전까지를 그 항목에 대한 말로 본다.
  // 판단할 말이 없으면 뒤 구간을 따른다 ("나이랑 직장 다 공개해")
  const verdictFrom = (at: number) => {
    for (let i = points.indexOf(at); i < points.length; i++) {
      const v = verdict(text.slice(points[i], points[i + 1] ?? text.length))
      if (v !== null) return v
    }
    return null
  }

  const decisions: ConsentDecision[] = []
  for (const item of items) {
    const at = marks.get(item.id)!
    const allow = at >= 0 ? verdictFrom(at) : restAt >= 0 ? verdictFrom(restAt) : false
    if (allow === null) return fail
    decisions.push({ item, allow })
  }
  return { ok: true, decisions }
}

/** 답변 예시. 예: "나이는 공개하고 직장은 공개하지 마" */
export function example(items: ConsentItem[]) {
  const [first, second] = items
  if (!second) return `${josa(first.label, '은/는')} 공개해도 돼`
  return `${josa(first.label, '은/는')} 공개하고 ${josa(second.label, '은/는')} 공개하지 마`
}
