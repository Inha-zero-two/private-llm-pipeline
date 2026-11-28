// 파이프라인이 답하는 데 걸린 시간 (API-11)

import type { Elapsed } from '@/types'

type RunTimes = {
  startedAt?: string
  endedAt?: string | null
  consents?: { createdAt?: string; respondedAt?: string | null }[]
}

/** 전체 시간에서 동의를 기다린 시간(해석 포함)을 뺀다. 시각이 없으면 null */
export function pipelineElapsed(run: RunTimes): Elapsed | null {
  if (!run.startedAt || !run.endedAt) return null
  let ms = Date.parse(run.endedAt) - Date.parse(run.startedAt)
  let consentExcluded = false
  for (const { createdAt, respondedAt } of run.consents ?? []) {
    if (!createdAt || !respondedAt) continue
    ms -= Date.parse(respondedAt) - Date.parse(createdAt)
    consentExcluded = true
  }
  return Number.isFinite(ms) ? { ms: Math.max(0, ms), consentExcluded } : null
}

export function elapsedLabel({ ms, consentExcluded }: Elapsed): string {
  const seconds = `${(ms / 1000).toFixed(1)}초`
  return consentExcluded ? `${seconds} (동의 대기·해석 제외)` : seconds
}
