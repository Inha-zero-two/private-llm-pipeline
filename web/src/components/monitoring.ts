import type { ConsentRecord, FrontierQuery, Turn } from '@/types'

/** UI용 표시 모델. 향후 API-11 응답은 이 구조로 변환하며 현재는 데모만 읽는다. */
export type RunMonitoring = {
  status: string
  step: string | null
  endReason: string | null
  errorCode: string | null
  frontierQueries: FrontierQuery[]
  consents: ConsentRecord[]
}

export function monitorTurn(turn: Turn, pending = false): RunMonitoring {
  return {
    status: turn.running
      ? pending
        ? '동의 대기'
        : '실행 중'
      : turn.notice?.kind === 'error'
        ? '오류'
        : turn.notice?.kind === 'stopped'
          ? '중지됨'
          : turn.notice?.kind === 'limited'
            ? '제한 종료'
            : '완료',
    step: turn.step,
    endReason: turn.running ? null : (turn.notice?.text ?? '데모 실행 완료'),
    // 데모에는 오류 코드가 없다. 안내 문구를 API 코드로 추정하지 않는다.
    errorCode: null,
    frontierQueries: turn.frontier,
    consents: turn.consents,
  }
}
