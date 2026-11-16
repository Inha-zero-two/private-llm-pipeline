import type { PrivacySetting, Turn } from '@/types'
import { ConsentHistory } from './RunLogPanel'
import { ShieldIcon } from './icons'
import styles from './PrivacyStatus.module.css'

export function PrivacyStatus({
  setting,
  turn,
  pending,
  onOpenSettings,
}: {
  setting: PrivacySetting
  turn?: Turn
  pending: boolean
  onOpenSettings: () => void
}) {
  const decisions =
    turn?.consents.flatMap((record) => (record.kind === 'answered' ? record.decisions : [])) ?? []
  const allowed = decisions.filter((decision) => decision.allow).length
  const denied = decisions.length - allowed
  const cancelled = turn?.consents.filter((record) => record.kind === 'cancelled').length ?? 0
  return (
    <section className={styles.panel} aria-label="개인정보 공개 상태">
      <div className={styles.row}>
        <ShieldIcon size={16} />
        <strong>개인정보 공개 상태</strong>
        <button onClick={onOpenSettings}>공개 허용 설정</button>
      </div>
      <p>
        {setting.rules.length === 0
          ? '저장된 허용 규칙 없음 · 미지정 정보는 기본 비공개'
          : `저장된 공개 규칙 ${setting.rules.length}개 · 미지정 정보는 기본 비공개`}
        {' · 설정 변경은 다음 질문부터 적용됩니다.'}
      </p>
      <div role="status">
        {pending
          ? '이번 질문: 추가 공개 동의 대기 중'
          : turn
            ? `이번 질문: 추가 동의 허용 ${allowed}개 · 거절 ${denied}개 · 취소 ${cancelled}건 (저장 설정과 별도)`
            : '질문별 추가 공개 동의는 해당 질문에만 적용됩니다.'}
      </div>
      {(setting.rules.length > 0 || (turn?.consents.length ?? 0) > 0) && (
        <details className={styles.details}>
          <summary>설정 및 이번 질문의 동의 기록 보기</summary>
          {setting.rules.length > 0 && (
            <>
              <p>현재 저장된 설정 (진행 중이거나 이전 질문에 소급 적용되지 않음)</p>
              <ul>
                {setting.rules.map((rule, i) => (
                  <li key={i}>
                    {rule.target} · {rule.action === 'allow' ? '허용' : '금지'}
                  </li>
                ))}
              </ul>
            </>
          )}
          {turn && turn.consents.length > 0 && (
            <>
              <p>이번 질문의 동의 기록</p>
              <ConsentHistory records={turn.consents} />
            </>
          )}
        </details>
      )}
    </section>
  )
}
