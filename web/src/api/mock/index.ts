// 백엔드가 나오기 전까지 쓰는 브라우저 안 가짜 앱 서버.
// 같은 Api 인터페이스를 구현하고, 실행은 설계서 흐름(①~⑧)대로 SSE 이벤트를 흘린다.
// 데이터는 메모리에만 있어 새로고침하면 사라진다.

import {
  ApiError,
  type Api,
  type ChatDetail,
  type ConsentResultValue,
  type EndReason,
  type Message,
  type PrivacySetting,
  type Rule,
  type Run,
  type RunEvent,
  type RunSubscription,
} from '../types'
import { buildScenario, LIMITED_TEXT } from './scenario'

type MockChat = {
  chatId: string
  title: string
  updatedAt: number
  messages: Message[]
  runs: Run[]
}

type MockRun = {
  chatId: string
  run: Run
  events: RunEvent[]
  subs: Set<RunSubscription>
  stopped: boolean
  /** 동의 대기 중이면 응답을 넘길 함수 */
  resolveConsent: ((result: ConsentResultValue) => void) | null
}

class StoppedError extends Error {}

const chats = new Map<string, MockChat>()
const runs = new Map<string, MockRun>()
let activeRunId: string | null = null
let setting: PrivacySetting = { settingId: null, text: '', rules: [], updatedAt: null }
let seq = 0
const nextId = (prefix: string) => `${prefix}${++seq}`

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
const latency = () => sleep(80 + Math.random() * 120)
const clone = <T>(v: T): T => structuredClone(v)

function getChatOr404(chatId: string) {
  const chat = chats.get(chatId)
  if (!chat) throw new ApiError(404, 'NOT_FOUND', '채팅을 찾을 수 없습니다.')
  return chat
}

function getRunOr404(runId: string) {
  const r = runs.get(runId)
  if (!r) throw new ApiError(404, 'NOT_FOUND', '실행을 찾을 수 없습니다.')
  return r
}

function emit(r: MockRun, event: Omit<RunEvent, 'id'>) {
  const e = { ...event, id: r.events.length + 1 } as RunEvent
  r.events.push(e)
  for (const sub of r.subs) sub.onEvent(e)
}

function addMessage(chat: MockChat, role: Message['role'], content: string) {
  const msg: Message = {
    messageId: nextId('m'),
    role,
    content,
    createdAt: new Date().toISOString(),
  }
  chat.messages.push(msg)
  chat.updatedAt = Date.now()
  return msg
}

async function execute(r: MockRun, question: string) {
  const chat = chats.get(r.chatId)!
  const scenario = buildScenario(question, setting.rules)
  let answer = ''
  let endReason: EndReason = 'info_enough'

  const checkpoint = () => {
    if (r.stopped) throw new StoppedError()
  }
  const step = async (label: string, ms = 700) => {
    checkpoint()
    emit(r, { type: 'step', data: { label } })
    await sleep(ms)
    checkpoint()
  }

  const finish = (reason: EndReason) => {
    const answerMsg = answer ? addMessage(chat, 'assistant', answer) : null
    r.run.status = 'finished'
    r.run.endReason = reason
    r.run.limited = LIMITED_TEXT[reason]
    r.run.answerMessageId = answerMsg?.messageId ?? null
    activeRunId = null
    emit(r, {
      type: 'done',
      data: { endReason: reason, limited: r.run.limited, answerMessageId: r.run.answerMessageId },
    })
  }

  try {
    await step('개인정보 검출 중')
    if (scenario.kind === 'error') {
      r.run.status = 'finished'
      activeRunId = null
      emit(r, {
        type: 'error',
        data: { code: 'PIPELINE_ERROR', message: '로컬 모델 출력 형식 검사에 실패했습니다.' },
      })
      return
    }
    await step('허용 설정 적용 중', 400)
    await step('필요한 정보 정리 중')

    let consented = false
    for (let round = 1; ; round++) {
      await step('답변 가능 여부 판단 중')

      if (scenario.kind === 'offline') {
        await step('외부 모델 연결 확인 중')
        endReason = 'offline'
        break
      }
      if (round > scenario.rounds) {
        endReason = scenario.kind === 'call_limit' ? 'call_limit' : 'info_enough'
        break
      }

      if (round === 1 && scenario.consentItems.length > 0) {
        emit(r, {
          type: 'consent_request',
          data: {
            items: scenario.consentItems,
            purpose: '질문에 맞는 구체적인 답변을 받기 위해 외부 모델에 함께 보냅니다.',
            benefit: '상황에 맞춘 더 정확한 답변을 받을 수 있습니다.',
            risk: '선택한 정보가 외부 서버로 전송됩니다.',
          },
        })
        emit(r, { type: 'step', data: { label: '공개 동의를 기다리는 중' } })
        const result = await new Promise<ConsentResultValue>((resolve) => {
          r.resolveConsent = resolve
        })
        r.resolveConsent = null
        emit(r, { type: 'consent_result', data: { result } })
        checkpoint()
        if (result === 'deny') {
          endReason = 'consent_denied'
          break
        }
        consented = true
      }

      await step('도움 요청문 작성 중')
      await step('누출 검사 중', 500)
      if (scenario.consentItems.length > 0 && !consented && round === 1) {
        await step('요청문 다시 작성 중 (누출 검사에서 걸림)')
        await step('누출 검사 중', 500)
      }

      const request = scenario.frontierRequest(round, consented)
      await step(`외부 모델에 묻는 중 (${round}회차)`, 1200)
      const frontierAnswer = scenario.frontierAnswer(round)
      r.run.frontierQueries.push({
        queryId: nextId('q'),
        round,
        request,
        answer: frontierAnswer,
        status: 'ok',
      })
      emit(r, { type: 'frontier', data: { round, request, answer: frontierAnswer } })
      await step('임시 메모 정리 중', 400)
    }

    await step('최종 답변 생성 중', 500)
    const full = scenario.finalAnswer
    for (let i = 0; i < full.length;) {
      checkpoint()
      const n = 2 + Math.floor(Math.random() * 4)
      const text = full.slice(i, i + n)
      i += n
      answer += text
      emit(r, { type: 'token', data: { text } })
      await sleep(25)
    }
    finish(endReason)
  } catch (e) {
    if (!(e instanceof StoppedError)) throw e
    // 최종 답변 도중 중지면 부분 답변을 남긴다 (2.4 stop())
    finish('stopped')
  }
}

export const mockApi: Api = {
  async getHealth() {
    await latency()
    return { localLLM: 'ready', frontier: 'online' }
  },

  async listChats() {
    await latency()
    return [...chats.values()]
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .map((c) => ({
        chatId: c.chatId,
        title: c.title,
        activeRunId: activeRunId && runs.get(activeRunId)?.chatId === c.chatId ? activeRunId : null,
      }))
  },

  async createChat() {
    await latency()
    const chatId = nextId('c')
    chats.set(chatId, { chatId, title: '새 채팅', updatedAt: Date.now(), messages: [], runs: [] })
    return { chatId }
  },

  async getChat(chatId) {
    await latency()
    const chat = getChatOr404(chatId)
    const detail: ChatDetail = {
      chatId: chat.chatId,
      title: chat.title,
      activeRunId: activeRunId && runs.get(activeRunId)?.chatId === chatId ? activeRunId : null,
      messages: chat.messages,
      runs: chat.runs,
    }
    return clone(detail)
  },

  async deleteChat(chatId) {
    await latency()
    getChatOr404(chatId)
    if (activeRunId && runs.get(activeRunId)?.chatId === chatId) {
      throw new ApiError(409, 'CHAT_RUNNING', '답변을 만드는 중인 채팅은 삭제할 수 없습니다.')
    }
    chats.delete(chatId)
  },

  async sendMessage(chatId, text) {
    await latency()
    const chat = getChatOr404(chatId)
    if (activeRunId) {
      throw new ApiError(409, 'RUN_IN_PROGRESS', '다른 채팅에서 답변을 만드는 중입니다.')
    }
    const isFirst = !chat.messages.some((m) => m.role === 'user')
    const userMsg = addMessage(chat, 'user', text)
    if (isFirst) chat.title = text.slice(0, 30)

    const run: Run = {
      runId: nextId('r'),
      userMessageId: userMsg.messageId,
      answerMessageId: null,
      status: 'running',
      endReason: null,
      limited: null,
      frontierQueries: [],
    }
    chat.runs.push(run)
    const r: MockRun = {
      chatId,
      run,
      events: [],
      subs: new Set(),
      stopped: false,
      resolveConsent: null,
    }
    runs.set(run.runId, r)
    activeRunId = run.runId
    void execute(r, text)
    return { runId: run.runId }
  },

  subscribeRun(runId, sub) {
    const r = runs.get(runId)
    if (!r) {
      setTimeout(sub.onClosed, 0)
      return () => {}
    }
    // 접속하면 처음부터 다시 보낸다 (A-01)
    const replay = [...r.events]
    let active = true
    setTimeout(() => {
      if (!active) return
      for (const e of replay) sub.onEvent(e)
      r.subs.add(sub)
    }, 0)
    return () => {
      active = false
      r.subs.delete(sub)
    }
  },

  async stopRun(runId) {
    await latency()
    const r = getRunOr404(runId)
    if (r.run.status === 'finished') {
      throw new ApiError(409, 'RUN_FINISHED', '이미 끝난 실행입니다.')
    }
    r.stopped = true
    r.resolveConsent?.('cancelled')
  },

  async answerConsent(runId, allow) {
    await latency()
    const r = getRunOr404(runId)
    if (!r.resolveConsent) {
      throw new ApiError(409, 'NO_PENDING_CONSENT', '응답을 기다리는 동의 요청이 없습니다.')
    }
    r.resolveConsent(allow ? 'allow' : 'deny')
  },

  async getPrivacySetting() {
    await latency()
    return clone(setting)
  },

  async savePrivacySetting(text) {
    await sleep(600) // 로컬 모델이 설정문을 해석하는 시간
    const trimmed = text.trim()
    const rules = trimmed ? parseRules(trimmed) : []
    if (trimmed && rules.length === 0) {
      return {
        ok: false,
        rules: [],
        message:
          '설정문을 해석하지 못했습니다. "직무는 공개해도 돼"처럼 대상과 허용 여부를 적어 주세요.',
      }
    }
    setting = {
      settingId: (setting.settingId ?? 0) + 1,
      text: trimmed,
      rules,
      updatedAt: new Date().toISOString(),
    }
    return { ok: true, rules, message: '' }
  },
}

// 실제로는 로컬 모델이 하는 일. 목업은 문장을 나눠 "공개하지/숨겨/말아" 여부만 본다.
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
