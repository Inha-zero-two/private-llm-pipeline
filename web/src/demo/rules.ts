// 공개 허용 설정문 해석 데모. 실제로는 로컬 모델이 하는 일이다.

import type { Rule } from '@/types'

export type InterpretResult = { ok: true; rules: Rule[] } | { ok: false; message: string }

export async function interpretSetting(text: string): Promise<InterpretResult> {
  await new Promise((r) => setTimeout(r, 600)) // 로컬 모델이 설정문을 해석하는 시간
  const trimmed = text.trim()
  if (!trimmed) return { ok: true, rules: [] }
  const rules = parseRules(trimmed)
  if (rules.length === 0) {
    return {
      ok: false,
      message:
        '설정문을 해석하지 못했습니다. "직무는 공개해도 돼"처럼 대상과 허용 여부를 적어 주세요.',
    }
  }
  return { ok: true, rules }
}

// 문장을 나눠 "공개하지/숨겨/말아" 여부만 본다
function parseRules(text: string): Rule[] {
  const rules: Rule[] = []
  for (const sentence of text.split(/[.\n]|그리고|[,，]/)) {
    const s = sentence.trim()
    if (!s) continue
    const deny = /(하지\s*마|숨겨|말아|금지|안\s*돼|보내지\s*마)/.test(s)
    const allow = /(공개|괜찮|보내도|허용|돼)/.test(s)
    if (!deny && !allow) continue
    const target = s
      .replace(/(은|는|이|가|도)\s.*$/, '')
      .replace(/\s*(정보)$/, '')
      .trim()
    if (!target) continue
    for (const t of target
      .split(/와|과|및/)
      .map((x) => x.trim())
      .filter(Boolean)) {
      rules.push({ target: t, scope: 'category', action: deny ? 'deny' : 'allow' })
    }
  }
  return rules
}
