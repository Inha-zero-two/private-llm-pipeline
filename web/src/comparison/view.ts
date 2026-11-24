// 비교 패널이 보여 줄 내용을 정한다. 화면 없이 검사할 수 있게 컴포넌트와 나눈다.

import type { Turn } from '../types.ts'
import type {
  ComparisonItem,
  ComparisonKind,
  ComparisonState,
  ComparisonStatus,
  RunningComparison,
} from './types.ts'

export const STATUS_LABEL: Record<ComparisonStatus, string> = {
  running: '생성 중',
  ok: '완료',
  truncated: '잘림',
  failed: '실패',
  stopped: '중지됨',
}

export const formatSeconds = (ms: number) => `${(ms / 1000).toFixed(1)}초`

/** "앞 대화" 표시. 개수만 보인다. 표시하지 않을 때는 null */
export function usedPairsLabel(item: ComparisonItem): string | null {
  const { usedPairs: used, droppedPairs: dropped } = item
  if (used === null) return null
  if (used === 0 && dropped === 0) return '앞 대화 없음'
  return `앞 대화 ${used}개 사용` + (dropped > 0 ? ` (길이 때문에 ${dropped}개 제외)` : '')
}

/** 패널에 보여 줄 질문. 고른 질문이 없으면 최근 질문이다 */
export function turnToShow(turns: Turn[], selectedRunId: number | null): Turn | null {
  const selected = selectedRunId === null ? undefined : turns.find((t) => t.runId === selectedRunId)
  return selected ?? turns.at(-1) ?? null
}

export type PanelView =
  { item: ComparisonItem; banner: string | null } | { item: null; message: string }

export function panelView(
  state: ComparisonState,
  chatId: string | null,
  turn: Turn | null,
  kind: ComparisonKind,
): PanelView {
  if (chatId === null || turn === null) {
    return { item: null, message: '질문을 보내면 비교 답변이 여기에 표시됩니다.' }
  }
  const chat = state.chats[chatId]
  const item = chat?.items.find((i) => i.runId === turn.runId && i.kind === kind)
  if (chat && item) {
    return { item, banner: chat.registered ? null : '이 채팅은 더 이상 비교하지 않습니다.' }
  }
  if (turn.runId === undefined && turn.running) {
    return { item: null, message: '질문을 보내는 중입니다.' }
  }
  if (!chat) {
    const failed = state.failedChats[chatId]
    return {
      item: null,
      message: failed ? '비교 결과를 불러오지 못했습니다.' : '비교 결과를 불러오는 중입니다.',
    }
  }
  if (!chat.registered && chat.items.length === 0) {
    return { item: null, message: '이 채팅은 비교하지 않습니다.' }
  }
  if (state.running?.chatId === chatId && state.running.runId === turn.runId) {
    return { item: null, message: '비교를 준비하는 중입니다.' }
  }
  return { item: null, message: '이 질문의 비교 기록이 없습니다.' }
}

export type SendState = {
  /** 이 채팅에서 만드는 중인 출력이 있다 (전송 대신 중지를 보인다) */
  running: boolean
  /** 파이프라인은 끝나고 비교만 남았다 */
  comparing: boolean
  /** 다른 채팅 때문에 보낼 수 없는 이유 */
  blockedReason: string | null
}

/** 다음 질문을 보낼 수 있는지. 세 출력이 모두 끝나야 한다 */
export function sendState(
  currentChatId: string | null,
  pipelineChatId: string | null,
  comparison: RunningComparison | null,
): SendState {
  const pipelineHere = pipelineChatId !== null && pipelineChatId === currentChatId
  const comparisonHere = comparison !== null && comparison.chatId === currentChatId
  const running = pipelineHere || comparisonHere
  let blockedReason: string | null = null
  if (!running && pipelineChatId !== null) {
    blockedReason = '다른 채팅에서 답변을 만드는 중입니다.'
  } else if (!running && comparison !== null) {
    blockedReason = '다른 채팅에서 비교 답변을 만드는 중입니다.'
  }
  return { running, comparing: comparisonHere && pipelineChatId === null, blockedReason }
}
