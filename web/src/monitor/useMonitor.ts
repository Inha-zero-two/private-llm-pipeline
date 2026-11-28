import { useEffect, useState } from 'react'
import { readDemo, readTarget, subscribeMonitor, targetFromUrl } from './bridge'
import { followMonitor } from './client'
import { demoState } from './demo'
import { emptyMonitor } from './model'
import type { MonitorState, MonitorTarget } from './types'

export function useMonitor() {
  const [target, setTarget] = useState<MonitorTarget | null>(null)
  const [state, setState] = useState<MonitorState>(emptyMonitor)
  useEffect(() => {
    let key = ''
    let stop: (() => void) | undefined
    let live = true
    let initialUrl = targetFromUrl()
    const sync = () => {
      const next = readTarget() ?? initialUrl
      initialUrl = null
      const nextKey = JSON.stringify(next)
      if (nextKey !== key) {
        stop?.()
        key = nextKey
        setTarget(next)
        setState(emptyMonitor())
        const url = new URL('/monitor', location.origin)
        if (next?.kind === 'remote') {
          url.searchParams.set('chatId', next.chatId)
          url.searchParams.set('runId', String(next.runId))
          stop = followMonitor(next, (value) => {
            if (live && key === nextKey) setState(value)
          })
        }
        history.replaceState(null, '', url)
      }
      if (next?.kind === 'demo') {
        const snapshot = readDemo()
        if (snapshot?.target.id === next.id) setState((previous) => demoState(previous, snapshot))
      }
    }
    const unsubscribe = subscribeMonitor(sync)
    sync()
    return () => {
      live = false
      stop?.()
      unsubscribe()
    }
  }, [])
  return { target, state }
}
