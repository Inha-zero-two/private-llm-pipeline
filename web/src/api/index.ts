import { httpApi } from './http'
import { mockApi } from './mock'
import type { Api } from './types'

// 백엔드가 준비되기 전까지는 목업을 쓴다. VITE_API_MODE=http 로 실제 서버에 붙는다.
export const api: Api = import.meta.env.VITE_API_MODE === 'http' ? httpApi : mockApi

export * from './types'
