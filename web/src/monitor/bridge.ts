import type { DemoSnapshot, MonitorTarget } from './types'

const KEY = 'pipeline.monitor.target.v1'
const CHANNEL = 'pipeline.monitor.v1'
let current: MonitorTarget | null = null
let demo: DemoSnapshot | null = null
let channel: BroadcastChannel | null = null
const listeners = new Set<() => void>()

export function parseTarget(value: string | null): MonitorTarget | null {
  try {
    const data = JSON.parse(value ?? 'null') as MonitorTarget | null
    if (!data || typeof data.chatId !== 'string') return null
    if (data.kind === 'remote' && Number.isSafeInteger(data.runId) && data.runId > 0)
      return { kind: 'remote', chatId: data.chatId, runId: data.runId }
    if (data.kind === 'demo' && typeof data.id === 'string') return data
  } catch {
    /* 저장 값이 손상되면 대기 화면 */
  }
  return null
}

function emit() {
  for (const listener of listeners) listener()
}

function connect() {
  if (channel || typeof BroadcastChannel === 'undefined') return
  channel = new BroadcastChannel(CHANNEL)
  channel.onmessage = ({ data }: MessageEvent) => {
    if (data?.type === 'request') {
      if (demo) channel?.postMessage({ type: 'demo', snapshot: demo })
      return
    }
    if (data?.type === 'target') {
      current = parseTarget(JSON.stringify(data.target))
      demo = null
      emit()
    }
    if (data?.type === 'demo' && data.snapshot?.target?.kind === 'demo') {
      const target = readTarget()
      if (target?.kind !== 'demo' || target.id !== data.snapshot.target.id) return
      demo = data.snapshot
      emit()
    }
  }
}

export function readTarget(): MonitorTarget | null {
  try {
    return parseTarget(localStorage.getItem(KEY)) ?? current
  } catch {
    return current
  }
}

export function publishTarget(target: MonitorTarget | null) {
  current = target
  demo = null
  connect()
  // 실제 대화 본문은 저장하지 않고 전달받은 실행 ID만 보관한다.
  try {
    if (target) localStorage.setItem(KEY, JSON.stringify(target))
    else localStorage.removeItem(KEY)
  } catch {
    /* 저장이 제한된 브라우저는 채널과 창 URL로 연결 */
  }
  channel?.postMessage({ type: 'target', target })
  emit()
}

export function publishDemo(snapshot: DemoSnapshot) {
  const target = readTarget()
  if (target?.kind !== 'demo' || target.id !== snapshot.target.id) return
  demo = snapshot
  connect()
  channel?.postMessage({ type: 'demo', snapshot })
  emit()
}

export function readDemo() {
  return demo
}

export function subscribeMonitor(listener: () => void) {
  connect()
  listeners.add(listener)
  const storage = (event: StorageEvent) => {
    if (event.key !== KEY) return
    current = parseTarget(event.newValue)
    demo = null
    emit()
    channel?.postMessage({ type: 'request' })
  }
  window.addEventListener('storage', storage)
  channel?.postMessage({ type: 'request' })
  return () => {
    listeners.delete(listener)
    window.removeEventListener('storage', storage)
  }
}

export function openMonitor(): boolean {
  const target = readTarget()
  const url = new URL('/monitor', location.origin)
  if (target?.kind === 'remote') {
    url.searchParams.set('runId', String(target.runId))
    url.searchParams.set('chatId', target.chatId)
  }
  return window.open(url.href, 'pipeline-monitor') !== null
}

export function targetFromUrl(): MonitorTarget | null {
  const params = new URLSearchParams(location.search)
  return parseTarget(
    JSON.stringify({
      kind: 'remote',
      runId: Number(params.get('runId')),
      chatId: params.get('chatId'),
    }),
  )
}
