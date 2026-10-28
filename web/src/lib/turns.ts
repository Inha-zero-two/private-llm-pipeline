import type { ChatDetail, ConsentItem, ConsentResultValue, Message, PDCategory } from '@/api'
import type { LiveRun } from '@/hooks/useLiveRun'

export const CATEGORY_LABEL: Record<PDCategory, string> = {
  0: '식별정보',
  1: '민감정보',
  2: '속성정보',
}

export type FrontierView = {
  round: number
  request: string
  answer: string | null
  status: 'ok' | 'failed' | 'uncertain'
}

export type NoticeView = { kind: 'limited' | 'stopped' | 'error'; text: string }

/** 질문 하나와 그에 대한 실행 결과 */
export type Turn = {
  key: string
  question: string
  running: boolean
  step: string | null
  frontier: FrontierView[]
  consentResults: { items: ConsentItem[]; result: ConsentResultValue }[]
  answer: string
  notice: NoticeView | null
}

function noticeOf(endReason: string | null, limited: string | null): NoticeView | null {
  if (!limited) return null
  return { kind: endReason === 'stopped' ? 'stopped' : 'limited', text: limited }
}

/** 저장된 채팅 기록과 진행 중인 실행을 화면에 그릴 순서로 합친다 */
export function buildTurns(detail: ChatDetail, live: LiveRun | null): Turn[] {
  const byId = new Map<string, Message>(detail.messages.map((m) => [m.messageId, m]))

  return detail.messages
    .filter((m) => m.role === 'user')
    .map((user): Turn => {
      const run = detail.runs.find((r) => r.userMessageId === user.messageId)
      const base = { key: user.messageId, question: user.content }

      if (run && live && live.runId === run.runId) {
        return {
          ...base,
          running: !live.done && !live.error,
          step: live.reconnecting ? '서버와 연결이 끊겨 다시 연결하는 중' : live.step,
          frontier: live.frontier.map((f) => ({ ...f, status: 'ok' as const })),
          consentResults: live.consentResults,
          answer: live.answer,
          notice: live.error
            ? { kind: 'error', text: live.error.message }
            : live.done
              ? noticeOf(live.done.endReason, live.done.limited)
              : null,
        }
      }

      const answer = run?.answerMessageId ? (byId.get(run.answerMessageId)?.content ?? '') : ''
      const running = run?.status === 'running'
      return {
        ...base,
        running,
        step: running ? '진행 상황을 불러오는 중' : null,
        frontier: run?.frontierQueries ?? [],
        consentResults: [],
        answer,
        notice:
          run && run.status === 'finished' && run.endReason === null
            ? { kind: 'error', text: '오류로 답변을 만들지 못했습니다.' }
            : noticeOf(run?.endReason ?? null, run?.limited ?? null),
      }
    })
}
