// 비교 상태 관리. 화면(React)과 상관없이 서버의 결과와 실시간 이벤트를 모은다.

import { comparisonApi } from './api.ts'
import type {
  ChatComparisons,
  ComparisonItem,
  ComparisonKind,
  ComparisonState,
  RunningComparison,
} from './types.ts'

/** 실시간 연결이 완전히 닫혔을 때 서버 상태를 다시 확인하기까지의 시간 */
const RECOVER_MS = 3000

const EMPTY: ComparisonState = { enabled: false, running: null, chats: {}, failedChats: {} }

function without<T>(record: Record<string, T>, key: string): Record<string, T> {
  return Object.fromEntries(Object.entries(record).filter(([k]) => k !== key))
}

export function createTracker(recoverMs = RECOVER_MS) {
  let state = EMPTY
  const listeners = new Set<() => void>()
  let source: EventSource | null = null
  let timer: ReturnType<typeof setTimeout> | undefined
  /** 받고 있는 실행 */
  let following: RunningComparison | null = null
  /** 받고 있는 실행의 snapshot을 받았는지. 그 뒤로는 조회보다 실시간 이벤트가 최신이다 */
  let received = false

  const set = (patch: Partial<ComparisonState>) => {
    state = { ...state, ...patch }
    for (const listener of listeners) listener()
  }

  const updateChat = (chatId: string, fn: (chat: ChatComparisons) => ChatComparisons) => {
    const chat = state.chats[chatId] ?? { registered: true, items: [] }
    set({ chats: { ...state.chats, [chatId]: fn(chat) } })
  }

  const updateItem = (
    target: RunningComparison,
    kind: ComparisonKind,
    fn: (item: ComparisonItem) => ComparisonItem,
  ) =>
    updateChat(target.chatId, (chat) => ({
      ...chat,
      items: chat.items.map((i) => (i.runId === target.runId && i.kind === kind ? fn(i) : i)),
    }))

  const close = () => {
    clearTimeout(timer)
    source?.close()
    source = null
    following = null
    received = false
  }

  const load = async (chatId: string) => {
    let data: ChatComparisons
    try {
      data = await comparisonApi.chat(chatId)
    } catch {
      set({ failedChats: { ...state.failedChats, [chatId]: true } })
      return
    }
    let items = data.items
    if (following?.chatId === chatId && received) {
      // 조회하는 동안 실시간으로 받은 글을 옛 값으로 덮지 않는다
      const { runId } = following
      const live = (state.chats[chatId]?.items ?? []).filter((i) => i.runId === runId)
      items = [...items.filter((i) => i.runId !== runId), ...live]
    }
    set({
      chats: { ...state.chats, [chatId]: { ...data, items } },
      failedChats: without(state.failedChats, chatId),
    })
    const running = data.items.find((i) => i.status === 'running')
    if (running && !following) follow({ chatId, runId: running.runId })
  }

  const finish = (target: RunningComparison) => {
    close()
    set({ running: null })
    void load(target.chatId)
  }

  /** 연결이 완전히 닫혔을 때: 아직 진행 중이면 다시 붙고, 끝났으면 결과를 불러온다 */
  const recover = (target: RunningComparison) => {
    clearTimeout(timer)
    timer = setTimeout(() => {
      comparisonApi
        .status()
        .then(({ running }) => {
          if (following !== target) return
          if (running?.runId === target.runId) follow(target)
          else finish(target)
        })
        .catch(() => {
          if (following === target) recover(target)
        })
    }, recoverMs)
  }

  const follow = (target: RunningComparison) => {
    close()
    following = target
    set({ running: target })
    const events = new EventSource(comparisonApi.eventsUrl(target.runId))
    source = events
    const on = (name: string, handle: (data: never) => void) =>
      events.addEventListener(name, (e) => {
        if (source === events) handle(JSON.parse((e as MessageEvent<string>).data) as never)
      })
    // 다시 연결되면 snapshot이 새로 온다. 이어 붙이지 않고 덮어쓴다
    on('snapshot', ({ items }: { items: ComparisonItem[] }) => {
      received = true
      updateChat(target.chatId, (chat) => ({
        ...chat,
        items: [...chat.items.filter((i) => i.runId !== target.runId), ...items],
      }))
    })
    on('token', ({ kind, text }: { kind: ComparisonKind; text: string }) =>
      updateItem(target, kind, (i) => ({ ...i, answer: (i.answer ?? '') + text })),
    )
    on('result', (item: ComparisonItem) => updateItem(target, item.kind, () => item))
    on('done', () => finish(target))
    events.addEventListener('error', () => {
      // 끊기면 EventSource가 스스로 다시 붙는다. 완전히 닫혔을 때만 확인한다
      if (source === events && events.readyState === EventSource.CLOSED) recover(target)
    })
  }

  /** 서버의 상태를 확인하고, 진행 중인 비교가 있으면 받는다 */
  const sync = async () => {
    const { enabled, running } = await comparisonApi.status()
    set({ enabled })
    if (!running || following?.runId === running.runId) return
    const target = { chatId: String(running.chatId), runId: running.runId }
    follow(target)
    await load(target.chatId)
  }

  return {
    get: () => state,
    subscribe: (listener: () => void) => {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    sync,
    load,
    /** 질문을 보내 실행 번호를 받은 뒤 부른다 */
    started: (chatId: string, runId: number) => {
      if (!state.enabled) {
        // 처음 확인에 실패했을 수 있다. 꺼져 있으면 아무 일도 하지 않는다
        sync().catch(() => {})
        return
      }
      follow({ chatId, runId })
      void load(chatId)
    },
    retry: async (chatId: string, runId: number, kind: ComparisonKind) => {
      await comparisonApi.retry(runId, kind)
      follow({ chatId, runId })
    },
    /** 진행 중인 비교를 중지한다. 이미 끝났으면(409) done이 상태를 정리한다 */
    stop: () => {
      if (state.running) comparisonApi.stop(state.running.runId).catch(() => {})
    },
    /** 삭제한 채팅의 결과를 지운다. 진행 중이었으면 done까지 받는다 */
    forget: (chatId: string) => set({ chats: without(state.chats, chatId) }),
    close,
  }
}
