import { useState } from 'react'
import { CATEGORY_LABEL, type FrontierView, type Turn as TurnData } from '@/lib/turns'
import { ChevronIcon, GlobeIcon, InfoIcon, ShieldIcon } from './icons'
import { Markdown } from './Markdown'
import styles from './Turn.module.css'

export function Turn({ turn }: { turn: TurnData }) {
  return (
    <section className={styles.turn}>
      <div className={styles.user}>
        <div className={styles.userBubble}>{turn.question}</div>
      </div>

      <div className={styles.assistant}>
        {turn.consentResults.map((c, i) => (
          <div key={i} className={styles.consentLine} data-result={c.result}>
            <ShieldIcon size={15} />
            {c.result === 'allow'
              ? '공개를 허용했습니다'
              : c.result === 'deny'
                ? '공개를 거절했습니다'
                : '동의 요청이 취소되었습니다'}
            <span className={styles.consentItems}>
              {c.items.map((item) => `${item.text} (${CATEGORY_LABEL[item.category]})`).join(', ')}
            </span>
          </div>
        ))}

        {turn.frontier.length > 0 && <FrontierToggle queries={turn.frontier} />}

        {turn.running && turn.step && (
          <div className={styles.step} role="status">
            <span className={styles.spinner} aria-hidden="true" />
            <span className={styles.stepLabel}>{turn.step}</span>
          </div>
        )}

        {turn.answer && (
          <div className={styles.answer} data-streaming={turn.running}>
            <Markdown>{turn.answer}</Markdown>
          </div>
        )}

        {turn.notice && (
          <div className={styles.notice} data-kind={turn.notice.kind}>
            <InfoIcon size={16} />
            {turn.notice.text}
          </div>
        )}
      </div>
    </section>
  )
}

function FrontierToggle({ queries }: { queries: FrontierView[] }) {
  // 기본은 닫힘 (설계서 9장)
  const [open, setOpen] = useState(false)

  return (
    <div className={styles.frontier}>
      <button
        className={styles.frontierHeader}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <GlobeIcon size={15} />
        외부 모델 질의 · 응답 {queries.length}건
        <ChevronIcon size={14} className={styles.chevron} data-open={open} />
      </button>

      {open && (
        <ol className={styles.frontierList}>
          {queries.map((q) => (
            <li key={q.round} className={styles.frontierItem}>
              <div className={styles.frontierRound}>
                {q.round}회차
                {q.status !== 'ok' && (
                  <span className={styles.frontierStatus}>
                    {q.status === 'failed' ? '전송 실패' : '전송 여부 불확실'}
                  </span>
                )}
              </div>
              <div className={styles.frontierLabel}>보낸 요청문</div>
              <p className={styles.frontierText}>{q.request}</p>
              <div className={styles.frontierLabel}>받은 응답</div>
              <p className={styles.frontierText}>{q.answer ?? '(응답 없음)'}</p>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}
