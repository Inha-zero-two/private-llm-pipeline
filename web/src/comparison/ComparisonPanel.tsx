import { useState } from 'react'
import type { ReactNode } from 'react'
import { Markdown } from '@/components/Markdown'
import type { Turn } from '@/types'
import styles from './ComparisonPanel.module.css'
import type { ComparisonItem, ComparisonKind, ComparisonState } from './types'
import { formatSeconds, panelView, STATUS_LABEL, usedPairsLabel } from './view'

type Props = {
  resizeHandle?: ReactNode
  state: ComparisonState
  chatId: string | null
  /** 결과를 보여 줄 질문 */
  turn: Turn | null
  /** 진행 중인 출력이 없어 다시 시도할 수 있는지 */
  canRetry: boolean
  onRetry: (runId: number, kind: ComparisonKind) => Promise<void>
}

/** 오른쪽 위아래 비교 패널. 위는 로컬 단독, 아래는 프론티어 단독 */
export function ComparisonPanel(props: Props) {
  return (
    <aside id="model-comparison" className={styles.panel} aria-label="모델 비교 결과">
      {props.resizeHandle}
      <Section {...props} kind="local" />
      <Section {...props} kind="frontier" />
    </aside>
  )
}

function Section({
  kind,
  state,
  chatId,
  turn,
  canRetry,
  onRetry,
}: Props & { kind: ComparisonKind }) {
  const title = `비교용 ${kind === 'local' ? 'Local SLM' : 'Frontier LLM'} 결과`
  const titleId = `${kind}-result-title`
  const view = panelView(state, chatId, turn, kind)
  const { item } = view

  return (
    <section className={styles.section} data-model={kind} aria-labelledby={titleId}>
      <header className={styles.header}>
        <h2 id={titleId}>{title}</h2>
        {item && (
          <span className={styles.badge} data-status={item.status} role="status">
            {STATUS_LABEL[item.status]}
          </span>
        )}
      </header>
      <div className={styles.scroll} tabIndex={0}>
        <p className={styles.placeholder}>같은 질문을 모델에 그대로 물은 답변입니다.</p>
        {view.item === null ? (
          <p className={styles.placeholder}>{view.message}</p>
        ) : (
          <>
            {view.banner && <p className={styles.banner}>{view.banner}</p>}
            <Result
              // 다른 질문이나 다시 시도한 결과로 바뀌면 오류 안내를 새로 시작한다
              key={`${view.item.runId}-${view.item.attempts}`}
              item={view.item}
              canRetry={canRetry}
              onRetry={onRetry}
            />
          </>
        )}
      </div>
    </section>
  )
}

function Result({
  item,
  canRetry,
  onRetry,
}: {
  item: ComparisonItem
  canRetry: boolean
  onRetry: Props['onRetry']
}) {
  const [retrying, setRetrying] = useState(false)
  const [retryError, setRetryError] = useState<string | null>(null)

  const retry = async () => {
    setRetrying(true)
    setRetryError(null)
    try {
      await onRetry(item.runId, item.kind)
    } catch (err) {
      setRetryError(err instanceof Error ? err.message : String(err))
    } finally {
      setRetrying(false)
    }
  }

  return (
    <article className={styles.result}>
      <h3>사용자 질문 원문</h3>
      <p>{item.question}</p>
      <h3>비교 답변</h3>
      <div className={styles.answer}>
        {item.answer ? (
          <Markdown>{item.answer}</Markdown>
        ) : (
          <span className={styles.muted}>
            {item.status === 'running' ? '생성 중' : '받은 답변이 없습니다.'}
          </span>
        )}
      </div>
      {item.message && (
        <p className={styles.error} role="alert">
          {item.message}
        </p>
      )}
      <div className={styles.footer}>
        <span>
          걸린 시간{' '}
          {item.status === 'running'
            ? STATUS_LABEL.running
            : item.elapsedMs === null
              ? '기록 없음'
              : formatSeconds(item.elapsedMs)}
        </span>
        {item.retryable && canRetry && (
          <button
            type="button"
            className={styles.retry}
            disabled={retrying}
            onClick={() => void retry()}
          >
            다시 시도
          </button>
        )}
      </div>
      {retryError && (
        <p className={styles.error} role="alert">
          {retryError}
        </p>
      )}
      <Details item={item} />
    </article>
  )
}

function Details({ item }: { item: ComparisonItem }) {
  const pairs = usedPairsLabel(item)
  const tokens =
    item.inputTokens === null && item.outputTokens === null
      ? '기록 없음'
      : `입력 ${item.inputTokens ?? '-'} · 출력 ${item.outputTokens ?? '-'}`
  return (
    <details className={styles.details}>
      <summary>자세히</summary>
      <dl>
        <dt>토큰</dt>
        <dd>{tokens}</dd>
        <dt>비용</dt>
        <dd>{item.cost === null ? '기록 없음' : `$${item.cost.toFixed(6)}`}</dd>
        <dt>모델</dt>
        <dd>{item.modelName ?? '기록 없음'}</dd>
        <dt>시도 횟수</dt>
        <dd>{item.attempts}회</dd>
        {pairs && (
          <>
            <dt>앞 대화</dt>
            <dd>{pairs}</dd>
          </>
        )}
      </dl>
    </details>
  )
}
