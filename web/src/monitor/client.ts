import { applyEvent, emptyMonitor, mergeDetail } from './model.ts'
import type { MonitorState, MonitorTarget, RunDetail } from './types.ts'

/** 한 실행만 구독한다. 해제는 조회/SSE만 중단하고 파이프라인에는 요청하지 않는다. */
export function followMonitor(
  target: Extract<MonitorTarget, { kind: 'remote' }>,
  update: (state: MonitorState) => void,
) {
  const abort = new AbortController()
  let disposed = false
  let source: EventSource | null = null
  let retry: ReturnType<typeof setTimeout> | null = null
  let state = emptyMonitor()
  let lastId = 0
  let terminal = false
  let syncing = false
  const emit = () => {
    if (!disposed) update(state)
  }
  const json = async <T>(path: string): Promise<T> => {
    const res = await fetch(path, { signal: abort.signal })
    if (!res.ok)
      throw new Error(
        res.status === 404 ? '실행 또는 채팅이 삭제되었습니다.' : `조회 실패 (${res.status})`,
      )
    return res.json() as Promise<T>
  }
  const runPath = `/api/runs/${target.runId}`
  const refresh = async () => {
    const run = await json<RunDetail>(runPath)
    if (disposed) return
    state = mergeDetail(state, run)
    emit()
  }
  const schedule = () => {
    if (disposed || retry) return
    retry = setTimeout(() => {
      retry = null
      void initialize()
    }, 2500)
  }
  const listen = () => {
    source = new EventSource(`${runPath}/events`)
    source.onopen = () => {
      state = { ...state, connection: 'connected', connectionMessage: null }
      emit()
      // 재접속 중 확정된 실패·중지도 상세에서 보완한다.
      if (lastId > 0) void refresh().catch(() => {})
    }
    for (const type of [
      'step',
      'frontier_request',
      'frontier_result',
      'consent_request',
      'consent_result',
      'done',
      'error',
    ]) {
      source.addEventListener(type, (event) => {
        if (!(event instanceof MessageEvent) || !event.data || disposed) return
        const id = Number(event.lastEventId)
        if (!Number.isSafeInteger(id) || id <= lastId) return
        let data: Record<string, unknown>
        try {
          data = JSON.parse(event.data)
        } catch {
          return
        }
        lastId = id
        state = applyEvent(state, type, data, id)
        if (type === 'done' || type === 'error') {
          terminal = true
          source?.close()
          state = { ...state, connection: 'finished', connectionMessage: null }
          void refresh().catch(() => {})
        }
        emit()
      })
    }
    source.addEventListener('replay_complete', () => {
      if (disposed) return
      state = { ...state, connection: 'connected', connectionMessage: null }
      emit()
    })
    source.onerror = (event) => {
      if (disposed || terminal || (event instanceof MessageEvent && event.data)) return
      state = {
        ...state,
        connection: 'reconnecting',
        connectionMessage: '모니터 연결이 끊겼습니다. 재연결 중입니다.',
      }
      emit()
      if (source?.readyState === EventSource.CLOSED) {
        source.close()
        source = null
        schedule()
      }
    }
  }
  async function initialize() {
    if (disposed || syncing || source) return
    syncing = true
    try {
      const [run, chat] = await Promise.all([
        json<RunDetail>(runPath),
        json<{ title: string; messages: { messageId: number; content: string }[] }>(
          `/api/chats/${encodeURIComponent(target.chatId)}`,
        ),
      ])
      if (disposed) return
      if (String(run.chatId) !== target.chatId) throw new Error('실행과 채팅이 일치하지 않습니다.')
      state = {
        ...mergeDetail(state, run),
        chatTitle: chat.title,
        question: chat.messages.find((m) => m.messageId === run.userMessageId)?.content ?? '',
      }
      terminal = false
      lastId = 0
      // 재시도는 기록을 처음부터 재생하되 이벤트 ID로 로그 중복을 제거한다.
      emit()
      listen()
    } catch (error) {
      if (disposed) return
      const missing = error instanceof Error && error.message.includes('삭제')
      state = {
        ...state,
        connection: missing ? 'missing' : 'reconnecting',
        connectionMessage: error instanceof Error ? error.message : '서버에 연결할 수 없습니다.',
      }
      emit()
      if (!missing) schedule()
    } finally {
      syncing = false
    }
  }
  emit()
  void initialize()
  return () => {
    disposed = true
    abort.abort()
    source?.close()
    if (retry) clearTimeout(retry)
  }
}
