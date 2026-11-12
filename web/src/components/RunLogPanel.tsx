import type { ConsentRecord, ConsentRequest, Turn } from '@/types'
import { monitorTurn } from './monitoring'
import styles from './RunLogPanel.module.css'

export function ConsentHistory({ records }: { records: ConsentRecord[] }) {
  return (
    <ul className={styles.consents}>
      {records.map((record, i) => (
        <li key={i}>
          {record.kind === 'cancelled' ? (
            <>요청 취소: {record.items.map((item) => item.label).join(', ')}</>
          ) : (
            <>
              <q>{record.answer}</q>
              <ul className={styles.consents}>
                {record.decisions.map(({ item, allow }) => (
                  <li key={item.id} data-allow={allow}>
                    {item.label} {item.text} · {allow ? '공개 동의' : '공개 거절'}
                  </li>
                ))}
              </ul>
            </>
          )}
        </li>
      ))}
    </ul>
  )
}

export function RunLogPanel({ turns, consent }: { turns: Turn[]; consent: ConsentRequest | null }) {
  return (
    <section className={styles.panel} aria-label="실행 로그">
      <h2 className={styles.heading}>
        실행 로그 <span>데모</span>
      </h2>
      <div className={styles.scroll} tabIndex={0}>
        {turns.length === 0 && <p className={styles.muted}>선택한 채팅의 실행 기록이 없습니다.</p>}
        {[...turns].reverse().map((turn, index) => {
          const log = monitorTurn(turn, consent !== null && turn.running)
          return (
            <article className={styles.entry} key={turn.id}>
              <h3>
                질문 {turns.length - index} · {log.status}
              </h3>
              <p className={styles.question} title={turn.question}>
                {turn.question}
              </p>
              <dl className={styles.facts}>
                <dt>현재 단계</dt>
                <dd>{log.step ?? (turn.running ? '시작 대기' : '실행 종료')}</dd>
                <dt>실행 회차</dt>
                <dd>외부 질의 {log.frontierQueries.length}회 응답 수신</dd>
                <dt>종료 사유</dt>
                <dd>{log.endReason ?? '실행 중'}</dd>
                <dt>오류</dt>
                <dd>{turn.notice?.kind === 'error' ? turn.notice.text : '기록 없음'}</dd>
                <dt>오류 코드</dt>
                <dd>{log.errorCode ?? '미제공'}</dd>
              </dl>
              {turn.running && consent && (
                <p className={styles.muted}>
                  동의 대기: {consent.items.map((item) => item.label).join(', ')}
                </p>
              )}
              <details>
                <summary>공개 동의 기록 {log.consents.length}건</summary>
                {log.consents.length > 0 ? (
                  <ConsentHistory records={log.consents} />
                ) : (
                  <p className={styles.muted}>기록 없음</p>
                )}
              </details>
            </article>
          )
        })}
      </div>
    </section>
  )
}
