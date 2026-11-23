import type { ConsentRecord, FrontierQuery, Turn } from '@/types'

/** 실시간 서버 실행·저장 기록·데모를 실행 로그의 표시 모델로 변환한다. */
export type RunMonitoring = {
  status: string
  step: string | null
  endReason: string | null
  errorCode: string | null
  frontierQueries: FrontierQuery[]
  consents: ConsentRecord[]
}

const END_REASONS: Record<string, { status: string; label: string }> = {
  info_enough: { status: '완료', label: '정보 충분' },
  call_limit: { status: '제한 종료', label: '외부 질의 횟수 한도 도달' },
  regen_limit: { status: '제한 종료', label: '요청문 재생성 한도 도달' },
  budget_limit: { status: '제한 종료', label: '비용 한도 초과' },
  offline: { status: '외부 연결 불가', label: '외부 모델 연결 불가' },
  stopped: { status: '중지됨', label: '사용자 중지' },
}

export function monitorTurn(turn: Turn, pending = false, demo = false): RunMonitoring {
  const reason =
    turn.endReason && Object.hasOwn(END_REASONS, turn.endReason)
      ? END_REASONS[turn.endReason]
      : undefined
  const legacyDenied = turn.endReason === 'consent_denied'
  const fallbackStatus =
    turn.notice?.kind === 'stopped'
      ? '중지됨'
      : turn.notice?.kind === 'limited'
        ? '제한 종료'
        : '완료'
  let status: string
  let endReason: string | null
  if (turn.running) {
    status = pending ? '동의 대기' : '실행 중'
    endReason = null
  } else if (turn.notice?.kind === 'error') {
    status = '오류'
    endReason = '오류 종료'
  } else if (reason) {
    status = reason.status
    endReason = reason.label
  } else if (legacyDenied) {
    status = '종료 (과거 기록)'
    endReason = '공개 거부로 종료 (과거 기록)'
  } else if (demo) {
    status = fallbackStatus
    endReason = turn.notice?.text ?? '데모 실행 완료'
  } else {
    status = '종료'
    endReason = turn.endReason ? `알 수 없는 종료 사유 (${turn.endReason})` : '종료 사유 미제공'
  }
  return {
    status,
    step: turn.step,
    endReason,
    // 데모에는 오류 코드가 없다. 안내 문구를 API 코드로 추정하지 않는다.
    errorCode: null,
    frontierQueries: turn.frontier,
    consents: turn.consents,
  }
}
