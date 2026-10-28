import type { ServerStatus } from '@/hooks/useServerStatus'
import { InfoIcon } from './icons'
import styles from './ServerBanner.module.css'

type Props = {
  status: ServerStatus
  onRetry: () => void
}

// 앱 서버 · 로컬 모델 · 외부 모델 연결 상태 안내. 문제가 없으면 아무것도 그리지 않는다
export function ServerBanner({ status, onRetry }: Props) {
  let kind: 'error' | 'warn' | 'info'
  let text: string
  let retry = false

  if (status.kind === 'down') {
    kind = 'error'
    text = `${status.message} 서버가 켜져 있는지 확인해 주세요.`
    retry = true
  } else if (status.kind === 'ok' && status.health.localLLM === 'down') {
    kind = 'error'
    text = '앱 서버가 로컬 모델에 연결하지 못했습니다. 질문을 처리할 수 없습니다.'
    retry = true
  } else if (status.kind === 'ok' && status.health.localLLM === 'loading') {
    kind = 'info'
    text = '로컬 모델을 불러오는 중입니다. 준비되면 질문할 수 있습니다.'
  } else if (status.kind === 'ok' && status.health.frontier === 'offline') {
    kind = 'warn'
    text = '외부 모델에 연결할 수 없어 로컬 모델로만 답합니다.'
  } else {
    return null
  }

  return (
    <div className={styles.banner} data-kind={kind} role="status">
      <InfoIcon size={16} />
      <span className={styles.text}>{text}</span>
      {retry && (
        <button className={styles.retry} onClick={onRetry}>
          다시 시도
        </button>
      )}
    </div>
  )
}
