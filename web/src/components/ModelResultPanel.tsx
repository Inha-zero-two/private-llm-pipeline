import styles from './ModelResultPanel.module.css'

/** 파이프라인 내부 호출 기록과 별개로 전달받는 비교 결과. 이 컴포넌트는 호출하지 않는다. */
export type ModelComparisonResult = {
  question: string
  answer: string
}

type ModelResultPanelProps = {
  localResult?: ModelComparisonResult | null
  frontierResult?: ModelComparisonResult | null
}

function ComparisonSection({
  model,
  result,
}: {
  model: 'local' | 'frontier'
  result?: ModelComparisonResult | null
}) {
  const title = `비교용 ${model === 'local' ? 'Local SLM' : 'Frontier LLM'} 결과`
  const titleId = `${model}-result-title`

  return (
    <section className={styles.section} data-model={model} aria-labelledby={titleId}>
      <header className={styles.header}>
        <h2 id={titleId}>{title}</h2>
        <span className={styles.badge}>{result ? '결과 수신' : '연결 전'}</span>
      </header>
      <div className={styles.scroll} tabIndex={0}>
        {result ? (
          <article className={styles.result}>
            <h3>사용자 질문 원문</h3>
            <p>{result.question}</p>
            <h3>비교 답변</h3>
            <p>{result.answer}</p>
          </article>
        ) : (
          <>
            <p className={styles.placeholder}>비교 결과 없음</p>
            <p className={styles.placeholder}>
              동일한 사용자 질문 원문에 대한 독립 비교 결과를 표시할 영역입니다. 아직 연결되지
              않았습니다.
            </p>
          </>
        )}
      </div>
    </section>
  )
}

export function ModelResultPanel({ localResult, frontierResult }: ModelResultPanelProps) {
  return (
    <aside className={styles.panel} aria-label="모델 비교 결과">
      <ComparisonSection model="local" result={localResult} />
      <ComparisonSection model="frontier" result={frontierResult} />
    </aside>
  )
}
