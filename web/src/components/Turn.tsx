import { useState } from 'react'
import { elapsedLabel } from '@/backend/elapsed'
import type { ConsentRecord, FrontierQuery, Turn as TurnData } from '@/types'
import { ChevronIcon, GlobeIcon, InfoIcon, ShieldIcon } from './icons'
import { Markdown } from './Markdown'
import styles from './Turn.module.css'

type Props = {
  turn: TurnData
  selected: boolean
  onSelect?: (runId: number) => void
}

export function Turn({ turn, selected, onSelect }: Props) {
  const { runId } = turn
  return (
    <section className={styles.turn}>
      <div className={styles.user}>
        {onSelect && runId !== undefined ? (
          <button
            type="button"
            className={styles.userBubble}
            data-selectable="true"
            aria-pressed={selected}
            title="이 질문의 비교 답변 보기"
            onClick={() => onSelect(runId)}
          >
            {turn.question}
          </button>
        ) : (
          <div className={styles.userBubble}>{turn.question}</div>
        )}
      </div>

      <div className={styles.assistant}>
        {turn.consents.map((c, i) => (
          <ConsentLine key={i} record={c} />
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

        <ElapsedLine turn={turn} />
      </div>
    </section>
  )
}

// 파이프라인이 답하는 데 걸린 시간. 비교 답변의 걸린 시간과 견주어 볼 수 있다
function ElapsedLine({ turn }: { turn: TurnData }) {
  if (turn.running) {
    // 데모에는 실행 ID가 없고 걸린 시간도 표시하지 않는다
    return turn.runId === undefined ? null : <div className={styles.elapsed}>생성 중</div>
  }
  if (!turn.elapsed) return null
  const state = turn.errorCode ? '오류 · ' : turn.endReason === 'stopped' ? '중지됨 · ' : ''
  return (
    <div className={styles.elapsed}>
      걸린 시간 {state}
      {elapsedLabel(turn.elapsed)}
    </div>
  )
}

// 공개 요청에 어떻게 답했는지 남긴다
function ConsentLine({ record }: { record: ConsentRecord }) {
  if (record.kind === 'cancelled') {
    return (
      <div className={styles.consent}>
        <div className={styles.consentHead}>
          <ShieldIcon size={15} />
          공개 요청이 취소되었습니다
        </div>
      </div>
    )
  }
  return (
    <div className={styles.consent}>
      <div className={styles.consentHead}>
        <ShieldIcon size={15} />
        공개 여부에 답했습니다
        <q className={styles.consentAnswer}>{record.answer}</q>
      </div>
      <ul className={styles.consentList}>
        {record.decisions.map((d) => (
          <li key={d.item.id} className={styles.consentItem} data-allow={d.allow}>
            <span className={styles.consentVerdict}>{d.allow ? '공개' : '비공개'}</span>
            {d.item.label} {d.item.text}
          </li>
        ))}
      </ul>
    </div>
  )
}

function FrontierToggle({ queries }: { queries: FrontierQuery[] }) {
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
              <div className={styles.frontierRound}>{q.round}회차</div>
              <div className={styles.frontierLabel}>보낸 요청문</div>
              <p className={styles.frontierText}>{q.request}</p>
              <div className={styles.frontierLabel}>받은 응답</div>
              <p className={styles.frontierText}>{q.answer}</p>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}
