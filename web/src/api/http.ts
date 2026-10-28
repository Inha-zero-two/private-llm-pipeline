import { ApiError, type Api, type RunEvent, type RunEventType } from './types'

// 앱 서버 주소.
// 개발: 비워 둔다. 같은 주소의 /api로 보내면 개발 서버가 로컬 앱 서버로 넘긴다.
// 운영: 웹(CloudFront)과 서버가 다른 곳에 있으므로 빌드할 때 https 서버 주소를 넣는다 (예: https://api.example.com).
const BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/+$/, '')

const DEFAULT_TIMEOUT_MS = 15_000
// 설정문 해석은 서버가 로컬 모델을 부르므로 오래 걸릴 수 있다
const LLM_TIMEOUT_MS = 120_000

const EVENT_TYPES: RunEventType[] = [
  'step',
  'frontier',
  'consent_request',
  'consent_result',
  'token',
  'done',
  'error',
]

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  let res: Response
  try {
    res = await fetch(BASE_URL + path, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    })
  } catch (e) {
    // 서버가 꺼져 있거나, 주소가 틀렸거나, CORS로 막힌 경우 모두 여기로 온다
    if (e instanceof DOMException && e.name === 'TimeoutError') {
      throw new ApiError(0, 'TIMEOUT', '앱 서버가 응답하지 않습니다.')
    }
    throw new ApiError(0, 'NETWORK', '앱 서버에 연결할 수 없습니다.')
  }
  if (!res.ok) {
    // A-08 오류 응답 본문 { code, message }
    const err = (await res.json().catch(() => null)) as { code?: string; message?: string } | null
    // 앱 서버 앞의 프록시 · 게이트웨이가 서버에 닿지 못한 경우
    if (!err?.code && [502, 503, 504].includes(res.status)) {
      throw new ApiError(res.status, 'NETWORK', '앱 서버에 연결할 수 없습니다.')
    }
    throw new ApiError(
      res.status,
      err?.code ?? 'UNKNOWN',
      err?.message ?? `요청 실패 (${res.status})`,
    )
  }
  const text = await res.text()
  return (text ? JSON.parse(text) : undefined) as T
}

export const httpApi: Api = {
  getHealth: () => request('GET', '/api/health', undefined, 5_000),
  listChats: () => request('GET', '/api/chats'),
  createChat: () => request('POST', '/api/chats'),
  getChat: (chatId) => request('GET', `/api/chats/${chatId}`),
  deleteChat: (chatId) => request('DELETE', `/api/chats/${chatId}`),
  sendMessage: (chatId, text) => request('POST', `/api/chats/${chatId}/messages`, { text }),
  stopRun: (runId) => request('POST', `/api/runs/${runId}/stop`),
  answerConsent: (runId, allow) => request('POST', `/api/runs/${runId}/consent`, { allow }),
  getPrivacySetting: () => request('GET', '/api/settings/privacy'),
  savePrivacySetting: (text) => request('PUT', '/api/settings/privacy', { text }, LLM_TIMEOUT_MS),

  subscribeRun(runId, { onEvent, onClosed, onConnection }) {
    // 끊기면 EventSource가 Last-Event-ID를 붙여 다시 접속하고, 서버가 이어서 보낸다 (A-01)
    const source = new EventSource(`${BASE_URL}/api/runs/${runId}/events`)
    let finished = false

    for (const type of EVENT_TYPES) {
      source.addEventListener(type, (e) => {
        const msg = e as MessageEvent<string>
        onEvent({ id: Number(msg.lastEventId), type, data: JSON.parse(msg.data) } as RunEvent)
        if (type === 'done' || type === 'error') {
          finished = true
          source.close()
        }
      })
    }
    source.onopen = () => onConnection?.(true)
    source.onerror = () => {
      if (finished) return
      // CONNECTING: 브라우저가 다시 접속하는 중. CLOSED: 재접속도 포기한 상태 (404 등)
      if (source.readyState === EventSource.CONNECTING) onConnection?.(false)
      else if (source.readyState === EventSource.CLOSED) onClosed()
    }

    return () => {
      finished = true
      source.close()
    }
  },
}
