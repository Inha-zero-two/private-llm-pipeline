// 화면이 쓰는 백엔드 약속. 서버(remote)와 브라우저 안 데모(demo)가 같은 모양을 지킨다.

import type { ConsentDecision, ConsentRequest, PrivacySetting, Rule, Turn } from '@/types'

export type RunHandlers = {
  /** 진행 중인 질문의 화면 상태를 바꾼다 */
  update: (fn: (turn: Turn) => Turn) => void
  /** 동의 요청을 띄우거나(요청) 닫는다(null) */
  onConsent: (request: ConsentRequest | null) => void
  onFinish: () => void
}

/** 동의 응답 해석 결과. 해석하지 못하면 ok=false로 다시 묻는다 */
export type ConsentReply =
  { ok: true; decisions: ConsentDecision[] } | { ok: false; message: string }

export type RunHandle = {
  stop: () => void
  answerConsent: (answer: string) => Promise<ConsentReply>
}

export type SettingReply = { ok: true; rules: Rule[] } | { ok: false; message: string }

export type Backend = {
  name: 'remote' | 'demo'
  /** 채팅 목록. 데모는 화면이 들고 있는 목록을 그대로 쓰므로 null */
  listChats: () => Promise<{ id: string; title: string }[] | null>
  /** 채팅의 질문·답변 기록. 데모는 null */
  loadTurns: (chatId: string) => Promise<Turn[] | null>
  createChat: () => Promise<string>
  deleteChat: (chatId: string) => Promise<void>
  /** 질문을 보내 실행을 시작한다. 실행 ID(서버)를 turn.runId에 채운다 */
  startRun: (
    chatId: string,
    question: string,
    setting: PrivacySetting,
    handlers: RunHandlers,
  ) => Promise<RunHandle>
  /** 새로고침 등으로 끊긴, 아직 진행 중인 실행의 스트림에 다시 붙는다 */
  attachRun: (runId: number, handlers: RunHandlers) => RunHandle
  loadSetting: () => Promise<PrivacySetting | null>
  saveSetting: (text: string) => Promise<SettingReply>
}

/** 서버가 {code, message}로 알려 준 오류 */
export class ApiError extends Error {
  status: number
  code: string
  constructor(status: number, code: string, message: string) {
    super(message)
    this.status = status
    this.code = code
  }
}
