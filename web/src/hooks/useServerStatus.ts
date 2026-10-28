import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, type Health } from '@/api'

export type ServerStatus =
  { kind: 'checking' } | { kind: 'ok'; health: Health } | { kind: 'down'; message: string }

// 문제가 없으면 드물게, 문제가 있으면 자주 확인한다
const OK_INTERVAL_MS = 30_000
const PROBLEM_INTERVAL_MS = 5_000

/**
 * 웹과 앱 서버는 따로 돌기 때문에, 서버 · 로컬 모델이 준비됐는지 주기적으로 확인한다 (A-11).
 * 끊겼다가 다시 연결되면 onRecover를 부른다.
 */
export function useServerStatus(onRecover: () => void) {
  const [status, setStatus] = useState<ServerStatus>({ kind: 'checking' })
  const wasDown = useRef(false)
  const onRecoverRef = useRef(onRecover)

  useEffect(() => {
    onRecoverRef.current = onRecover
  })

  const check = useCallback(async () => {
    try {
      const health = await api.getHealth()
      setStatus({ kind: 'ok', health })
      if (wasDown.current) onRecoverRef.current()
      wasDown.current = false
    } catch (e) {
      setStatus({
        kind: 'down',
        message: e instanceof ApiError ? e.message : '앱 서버에 연결할 수 없습니다.',
      })
      wasDown.current = true
    }
  }, [])

  useEffect(() => {
    const healthy = status.kind === 'ok' && status.health.localLLM === 'ready'
    const delay = status.kind === 'checking' ? 0 : healthy ? OK_INTERVAL_MS : PROBLEM_INTERVAL_MS
    const t = setTimeout(() => void check(), delay)
    return () => clearTimeout(t)
  }, [status, check])

  return { status, recheck: check }
}

/** 질문을 보낼 수 없는 이유. 보낼 수 있으면 null */
export function serverBlockReason(status: ServerStatus): string | null {
  if (status.kind === 'down') return '앱 서버에 연결할 수 없습니다.'
  if (status.kind === 'ok' && status.health.localLLM === 'loading') {
    return '로컬 모델을 불러오는 중입니다.'
  }
  if (status.kind === 'ok' && status.health.localLLM === 'down') {
    return '로컬 모델에 연결할 수 없습니다.'
  }
  return null
}
