// npm run dev → 앱 서버 연결, npm run dev:demo → 브라우저 안 데모
import { demoBackend } from './demo'
import { remoteBackend } from './remote'

export const backend = import.meta.env.MODE === 'demo' ? demoBackend : remoteBackend
export { ApiError } from './types'
export type { ConsentReply, RunHandle } from './types'
