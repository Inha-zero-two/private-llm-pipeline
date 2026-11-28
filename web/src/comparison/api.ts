// 비교 API 호출. 실시간 이벤트는 tracker가 EventSource로 받는다.

import { api } from '../backend/remote.ts'
import type { ChatComparisons, ComparisonKind } from './types.ts'

type ApiStatus = { enabled: boolean; running: { chatId: number; runId: number } | null }

export const comparisonApi = {
  status: () => api<ApiStatus>('GET', '/api/comparisons/status'),
  chat: (chatId: string) => api<ChatComparisons>('GET', `/api/chats/${chatId}/comparisons`),
  retry: (runId: number, kind: ComparisonKind) =>
    api<null>('POST', `/api/runs/${runId}/comparisons/${kind}/retry`),
  stop: (runId: number) => api<null>('POST', `/api/runs/${runId}/stop`),
  eventsUrl: (runId: number) => `/api/runs/${runId}/comparisons/events`,
}
