// 앱 서버 연결 (docs/screen-api-spec.md API-01 ~ 11). vite 프록시가 /api를 서버로 보낸다.

import type {
  ConsentDecision,
  ConsentItem,
  ConsentRecord,
  FrontierQuery,
  Notice,
  PDCategory,
  PrivacySetting,
  Rule,
  Turn,
} from '@/types'
import { ApiError, type Backend, type RunHandle, type RunHandlers } from './types'

// 서버 응답 형식 (명세서 그대로)

type ApiItem = { pdId: number; label: string; text: string; category: PDCategory }
type ApiDecision = { pdId: number; allow: boolean }
type ApiMessage = { messageId: number; role: 'user' | 'assistant'; content: string }
type ApiRunSummary = {
  runId: number
  userMessageId: number
  status: 'running' | 'finished'
  notice: string | null
}
type ApiChatDetail = {
  chatId: number
  title: string
  messages: ApiMessage[]
  runs: ApiRunSummary[]
}
type ApiRunDetail = {
  runId: number
  status: 'running' | 'finished'
  endReason: string | null
  notice: string | null
  errorCode: string | null
  frontierQueries: { round: number; request: string; answer: string | null }[]
  consents: {
    items: ApiItem[]
    result: 'answered' | 'cancelled' | null
    responseText: string | null
    decisions: ApiDecision[]
  }[]
}
type ApiSetting = { settingId: number | null; text: string; rules: Rule[] }

async function api<T>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    throw new ApiError(
      0,
      'NETWORK_ERROR',
      '서버에 연결할 수 없습니다. 서버가 켜져 있는지 확인해 주세요.',
    )
  }
  const text = await res.text()
  const data = text ? JSON.parse(text) : null
  if (!res.ok) {
    throw new ApiError(
      res.status,
      data?.code ?? 'HTTP_ERROR',
      data?.message ?? `요청 실패 (${res.status})`,
    )
  }
  return data as T
}

const toItem = (i: ApiItem): ConsentItem => ({
  id: i.pdId,
  label: i.label,
  text: i.text,
  category: i.category,
})

function toDecisions(items: ConsentItem[], decisions: ApiDecision[]): ConsentDecision[] {
  return decisions.flatMap((d) => {
    const item = items.find((i) => i.id === d.pdId)
    return item ? [{ item, allow: d.allow }] : []
  })
}

function noticeOf(
  endReason: string | null,
  errorCode: string | null,
  text: string | null,
): Notice | null {
  if (!text) return null
  if (errorCode) return { kind: 'error', text }
  return { kind: endReason === 'stopped' ? 'stopped' : 'limited', text }
}

/** API-06 스트림을 받아 화면 상태로 옮긴다. 같은 실행을 처음부터 다시 받아도 결과가 같다 */
function follow(runId: number, h: RunHandlers): RunHandle {
  const source = new EventSource(`/api/runs/${runId}/events`)
  let consentId: number | null = null
  let consentItems: ConsentItem[] = []
  let lastId = 0
  let finished = false

  const finish = (notice: Notice | null) => {
    if (finished) return
    finished = true
    source.close()
    h.update((t) => ({ ...t, running: false, step: null, notice }))
    h.onConsent(null)
    h.onFinish()
  }

  // 기록 이벤트는 ID가 있다. 재접속으로 이미 받은 ID가 다시 오면 무시한다
  const on = (name: string, handle: (data: never) => void) =>
    source.addEventListener(name, (e) => {
      const msg = e as MessageEvent<string>
      const id = Number(msg.lastEventId)
      if (msg.lastEventId && id <= lastId) return
      if (msg.lastEventId) lastId = id
      handle(JSON.parse(msg.data) as never)
    })

  on('step', (d: { label: string }) => h.update((t) => ({ ...t, step: d.label })))
  on('frontier', (d: { request: string; answer: string }) =>
    h.update((t) => ({
      ...t,
      frontier: [...t.frontier, { round: t.frontier.length + 1, ...d }],
    })),
  )
  on(
    'consent_request',
    (d: {
      consentId: number
      items: ApiItem[]
      purpose: string
      benefit: string
      risk: string
    }) => {
      consentId = d.consentId
      consentItems = d.items.map(toItem)
      h.onConsent({
        consentId: d.consentId,
        items: consentItems,
        purpose: d.purpose,
        benefit: d.benefit,
        risk: d.risk,
      })
    },
  )
  on(
    'consent_result',
    (d: {
      consentId: number
      result: 'answered' | 'cancelled'
      text?: string
      decisions?: ApiDecision[]
    }) => {
      const record: ConsentRecord =
        d.result === 'answered'
          ? {
              kind: 'answered',
              answer: d.text ?? '',
              decisions: toDecisions(consentItems, d.decisions ?? []),
            }
          : { kind: 'cancelled', items: consentItems }
      if (d.consentId === consentId) consentId = null
      h.onConsent(null)
      h.update((t) => ({ ...t, consents: [...t.consents, record] }))
    },
  )
  on('token', (d: { text: string }) => h.update((t) => ({ ...t, answer: t.answer + d.text })))
  on('done', (d: { endReason: string; limited: string | null }) =>
    finish(noticeOf(d.endReason, null, d.limited)),
  )
  source.addEventListener('error', (e) => {
    // 서버가 보낸 error 이벤트(data 있음)와 연결 오류(data 없음)를 구분한다
    if (e instanceof MessageEvent && e.data) {
      const d = JSON.parse(e.data) as { message: string }
      finish({ kind: 'error', text: d.message })
      return
    }
    // 연결이 끊기면 EventSource가 마지막 ID로 다시 붙는다. 완전히 닫혔을 때만 상태를 확인한다
    if (source.readyState === EventSource.CLOSED && !finished) {
      api<ApiRunDetail>('GET', `/api/runs/${runId}`)
        .then((run) =>
          finish(
            run.status === 'finished'
              ? noticeOf(run.endReason, run.errorCode, run.notice)
              : { kind: 'error', text: '진행 상황 연결이 끊겼습니다. 채팅을 다시 열어 주세요.' },
          ),
        )
        .catch((err: ApiError) => finish({ kind: 'error', text: err.message }))
    }
  })

  return {
    stop: () => {
      api('POST', `/api/runs/${runId}/stop`).catch(() => {
        // 이미 끝났으면(409) 스트림의 done이 상태를 정리한다
      })
    },
    answerConsent: async (answer) => {
      if (consentId === null) return { ok: false, message: '응답할 동의 요청이 없습니다.' }
      try {
        const res = await api<{ ok: boolean; decisions?: ApiDecision[]; message: string }>(
          'POST',
          `/api/runs/${runId}/consent`,
          { consentId, text: answer },
        )
        // 동의 창은 스트림의 consent_result가 닫는다
        return res.ok
          ? { ok: true, decisions: toDecisions(consentItems, res.decisions ?? []) }
          : { ok: false, message: res.message }
      } catch (err) {
        return { ok: false, message: (err as ApiError).message }
      }
    },
  }
}

/** API-03 + API-11로 저장된 질문·답변을 화면의 Turn 목록으로 복원한다 */
async function loadTurns(chatId: string): Promise<Turn[]> {
  const chat = await api<ApiChatDetail>('GET', `/api/chats/${chatId}`)
  const details = await Promise.all(
    chat.runs.map((r) => api<ApiRunDetail>('GET', `/api/runs/${r.runId}`)),
  )
  const runByMessage = new Map(chat.runs.map((r, i) => [r.userMessageId, details[i]]))

  const turns: Turn[] = []
  for (const m of chat.messages) {
    if (m.role === 'assistant') {
      const last = turns.at(-1)
      if (last) last.answer = m.content
      continue
    }
    const run = runByMessage.get(m.messageId)
    const running = run?.status === 'running'
    turns.push({
      id: `m${m.messageId}`,
      runId: run?.runId,
      question: m.content,
      running,
      step: null,
      // 진행 중인 실행은 스트림을 처음부터 다시 받아 채운다
      frontier: running
        ? []
        : (run?.frontierQueries ?? []).map((q): FrontierQuery => ({
            round: q.round,
            request: q.request,
            answer: q.answer ?? '',
          })),
      consents: running
        ? []
        : (run?.consents ?? []).flatMap((c): ConsentRecord[] => {
            const items = c.items.map(toItem)
            if (c.result === 'answered') {
              return [
                {
                  kind: 'answered',
                  answer: c.responseText ?? '',
                  decisions: toDecisions(items, c.decisions),
                },
              ]
            }
            return c.result === 'cancelled' ? [{ kind: 'cancelled', items }] : []
          }),
      answer: '',
      notice: run && !running ? noticeOf(run.endReason, run.errorCode, run.notice) : null,
    })
  }
  return turns
}

export const remoteBackend: Backend = {
  name: 'remote',
  listChats: async () => {
    const chats = await api<{ chatId: number; title: string }[]>('GET', '/api/chats')
    return chats.map((c) => ({ id: String(c.chatId), title: c.title }))
  },
  loadTurns,
  createChat: async () => String((await api<{ chatId: number }>('POST', '/api/chats')).chatId),
  deleteChat: async (chatId) => {
    await api('DELETE', `/api/chats/${chatId}`)
  },
  startRun: async (chatId, question, _setting, handlers) => {
    const { runId } = await api<{ runId: number }>('POST', `/api/chats/${chatId}/messages`, {
      text: question,
    })
    handlers.update((t) => ({ ...t, runId }))
    return follow(runId, handlers)
  },
  attachRun: (runId, handlers) => follow(runId, handlers),
  loadSetting: async (): Promise<PrivacySetting> => {
    const s = await api<ApiSetting>('GET', '/api/settings/privacy')
    return { text: s.text, rules: s.rules }
  },
  saveSetting: async (text) => {
    try {
      const res = await api<{ ok: boolean; rules: Rule[]; message: string }>(
        'PUT',
        '/api/settings/privacy',
        { text },
      )
      return res.ok ? { ok: true, rules: res.rules } : { ok: false, message: res.message }
    } catch (err) {
      return { ok: false, message: (err as ApiError).message }
    }
  },
}
