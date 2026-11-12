import type { Turn } from '@/types'
import styles from './ModelResultPanel.module.css'

export function ModelResultPanel({ turns }: { turns: Turn[] }) {
  const count = turns.reduce((n, turn) => n + turn.frontier.length, 0)
  return (
    <aside className={styles.panel} aria-label="모델 결과">
      <section className={styles.section} aria-labelledby="local-result-title">
        <header className={styles.header}>
          <h2 id="local-result-title">Local SLM 결과</h2>
          <span className={styles.badge}>연결 전</span>
        </header>
        <div className={styles.scroll} tabIndex={0}>
          <p className={styles.placeholder}>
            내부 출력 API 연결 전입니다. 표시할 Local SLM 출력이 없습니다.
          </p>
        </div>
      </section>
      <section className={styles.section} aria-labelledby="frontier-result-title">
        <header className={styles.header}>
          <h2 id="frontier-result-title">Frontier LLM 결과</h2>
          <span className={styles.badge}>{count > 0 ? `데모 ${count}건` : '대기'}</span>
        </header>
        <div className={styles.scroll} tabIndex={0}>
          <p className={styles.placeholder}>
            선택한 채팅의 데모 질의·응답입니다. 실제 모델 API는 연결되지 않았습니다.
          </p>
          {count === 0 && <p className={styles.placeholder}>아직 수신한 데모 응답이 없습니다.</p>}
          {turns.map((turn, index) =>
            turn.frontier.map((query) => (
              <article className={styles.result} key={`${turn.id}-${query.round}`}>
                <h3>
                  질문 {index + 1} · {query.round}회차{' '}
                  <span className={styles.badge}>응답 수신</span>
                </h3>
                <h4>보낸 요청문</h4>
                <p>{query.request}</p>
                <h4>받은 응답</h4>
                <p>{query.answer}</p>
              </article>
            )),
          )}
        </div>
      </section>
    </aside>
  )
}
