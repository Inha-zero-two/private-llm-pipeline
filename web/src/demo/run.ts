// 화면 확인용 데모 실행. 설계서 흐름(①~⑧)대로 단계를 바꿔 가며 답변을 채운다.

import type { ConsentRecord, ConsentRequest, Notice, Rule, Turn } from '@/types'
import { interpretConsent, type ConsentInterpretation } from './consent'
import { buildScenario, LIMITED_TEXT } from './scenario'

export type DemoHandlers = {
  /** 진행 중인 질문의 화면 상태를 바꾼다 */
  update: (fn: (turn: Turn) => Turn) => void
  /** 동의 요청을 띄우거나(요청) 닫는다(null) */
  onConsent: (request: ConsentRequest | null) => void
  onFinish: () => void
}

export type DemoRun = {
  stop: () => void
  /** 동의 요청에 자연어로 답한다. 해석하지 못하면 ok=false로 다시 묻는다 */
  answerConsent: (answer: string) => Promise<ConsentInterpretation>
}

class StoppedError extends Error {}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

export function runDemo(question: string, rules: Rule[], h: DemoHandlers): DemoRun {
  const scenario = buildScenario(question, rules)
  let stopped = false
  let resolveConsent: ((record: ConsentRecord) => void) | null = null

  const checkpoint = () => {
    if (stopped) throw new StoppedError()
  }
  const step = async (label: string, ms = 700) => {
    checkpoint()
    h.update((t) => ({ ...t, step: label }))
    await sleep(ms)
    checkpoint()
  }
  const finish = (notice: Notice | null) => {
    h.update((t) => ({ ...t, running: false, step: null, notice }))
    h.onConsent(null)
    h.onFinish()
  }
  const limited = (text: string | null): Notice | null => (text ? { kind: 'limited', text } : null)

  const execute = async () => {
    try {
      await step('개인정보 검출 중')
      if (scenario.kind === 'error') {
        finish({ kind: 'error', text: '로컬 모델 출력 형식 검사에 실패했습니다.' })
        return
      }
      await step('허용 설정 적용 중', 400)
      await step('필요한 정보 정리 중')

      let endText: string | null = null
      const allowed = new Set<number>()
      for (let round = 1; ; round++) {
        await step('답변 가능 여부 판단 중')

        if (scenario.kind === 'offline') {
          await step('외부 모델 연결 확인 중')
          endText = LIMITED_TEXT.offline
          break
        }
        if (round > scenario.rounds) {
          if (scenario.kind === 'call_limit') endText = LIMITED_TEXT.call_limit
          break
        }

        if (round === 1 && scenario.consentItems.length > 0) {
          const items = scenario.consentItems
          h.update((t) => ({ ...t, step: '공개 동의를 기다리는 중' }))
          h.onConsent({
            items,
            purpose: '질문에 맞는 구체적인 답변을 받기 위해 외부 모델에 함께 보냅니다.',
            benefit: '상황에 맞춘 더 정확한 답변을 받을 수 있습니다.',
            risk: '선택한 정보가 외부 서버로 전송됩니다.',
          })
          const record = await new Promise<ConsentRecord>((resolve) => {
            resolveConsent = resolve
          })
          resolveConsent = null
          h.onConsent(null)
          h.update((t) => ({ ...t, consents: [...t.consents, record] }))
          checkpoint()
          if (record.kind === 'answered') {
            for (const d of record.decisions) if (d.allow) allowed.add(d.item.id)
          }
          // 하나도 공개하지 않으면 거절과 같다
          if (allowed.size === 0) {
            endText = LIMITED_TEXT.consent_denied
            break
          }
        }

        await step('도움 요청문 작성 중')
        await step('누출 검사 중', 500)

        const request = scenario.frontierRequest(round, allowed)
        await step(`외부 모델에 묻는 중 (${round}회차)`, 1200)
        const answer = scenario.frontierAnswer(round)
        h.update((t) => ({ ...t, frontier: [...t.frontier, { round, request, answer }] }))
        await step('임시 메모 정리 중', 400)
      }

      await step('최종 답변 생성 중', 500)
      const full = scenario.finalAnswer
      for (let i = 0; i < full.length;) {
        checkpoint()
        const n = 2 + Math.floor(Math.random() * 4)
        const text = full.slice(i, i + n)
        i += n
        h.update((t) => ({ ...t, answer: t.answer + text }))
        await sleep(25)
      }
      finish(limited(endText))
    } catch (e) {
      if (!(e instanceof StoppedError)) throw e
      // 최종 답변 도중 중지면 부분 답변을 남긴다 (2.4 stop())
      finish({ kind: 'stopped', text: LIMITED_TEXT.stopped })
    }
  }

  void execute()

  return {
    stop: () => {
      stopped = true
      resolveConsent?.({ kind: 'cancelled', items: scenario.consentItems })
    },
    answerConsent: async (answer) => {
      const result = await interpretConsent(answer, scenario.consentItems)
      if (result.ok) resolveConsent?.({ kind: 'answered', answer, decisions: result.decisions })
      return result
    },
  }
}
