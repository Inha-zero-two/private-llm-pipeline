// 브라우저 안 데모. 서버 없이 화면만 볼 때 쓴다 (npm run dev:demo).

import { runDemo } from '@/demo/run'
import { interpretSetting } from '@/demo/rules'
import type { Backend } from './types'

let seq = 0

export const demoBackend: Backend = {
  name: 'demo',
  listChats: async () => null,
  loadTurns: async () => null,
  createChat: async () => `demo-${++seq}`,
  deleteChat: async () => {},
  startRun: async (_chatId, question, setting, handlers) =>
    runDemo(question, setting.rules, handlers),
  attachRun: () => {
    throw new Error('데모에는 다시 붙을 실행이 없습니다.')
  },
  loadSetting: async () => null,
  saveSetting: (text) => interpretSetting(text),
}
