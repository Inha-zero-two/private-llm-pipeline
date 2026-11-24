import { useEffect, useSyncExternalStore } from 'react'
import { createTracker } from './tracker.ts'

const tracker = createTracker()

/** 비교 상태. active가 아니면(데모) 서버에 묻지 않고 항상 꺼진 상태다 */
export function useComparison(active: boolean) {
  const state = useSyncExternalStore(tracker.subscribe, tracker.get)
  useEffect(() => {
    if (active) tracker.sync().catch(() => {})
  }, [active])
  return { state, tracker }
}
