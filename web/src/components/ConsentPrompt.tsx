import { useState } from 'react'
import { example } from '@/demo/consent'
import type { ConsentInterpretation } from '@/demo/consent'
import { josa } from '@/lib/josa'
import { CATEGORY_LABEL, type ConsentRequest } from '@/types'
import { ShieldIcon } from './icons'
import styles from './ConsentPrompt.module.css'

type Props = {
  request: ConsentRequest
  onAnswer: (answer: string) => Promise<ConsentInterpretation>
}

const QUICK_ANSWERS = ['모두 공개해도 돼', '모두 공개하지 마']

// 추가 개인정보 공개 요청 (설계서 9장 8.2). 입력창 바로 위에 붙고, 사용자는 자연어로 답한다.
// 시간 제한이 없고, 답하거나 중지해야 사라진다.
export function ConsentPrompt({ request, onAnswer }: Props) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const labels = request.items.map((item) => item.label)
  const question = `${labels.slice(0, -1).join(', ')}${labels.length > 1 ? ', ' : ''}${josa(labels.at(-1)!, '을/를')}`

  const submit = async (answer: string) => {
    if (!answer.trim() || busy) return
    setBusy(true)
    setError('')
    const result = await onAnswer(answer.trim())
    setBusy(false)
    if (!result.ok) setError(result.message)
  }

  return (
    <section className={styles.box} aria-label="추가 개인정보 공개 요청">
      <div className={styles.head}>
        <ShieldIcon size={16} />
        추가 개인정보 공개 요청
      </div>

      <p className={styles.question}>
        <strong>{question}</strong> 외부 모델에 공개해도 될까요?
      </p>

      <ul className={styles.items}>
        {request.items.map((item) => (
          <li key={item.id} className={styles.item}>
            <span className={styles.category} data-category={item.category}>
              {CATEGORY_LABEL[item.category]}
            </span>
            <span className={styles.label}>{item.label}</span>
            {item.text}
          </li>
        ))}
      </ul>

      <dl className={styles.details}>
        <dt>목적</dt>
        <dd>{request.purpose}</dd>
        <dt>이점</dt>
        <dd>{request.benefit}</dd>
        <dt>불이익</dt>
        <dd>{request.risk}</dd>
      </dl>

      <form
        className={styles.answer}
        onSubmit={(e) => {
          e.preventDefault()
          void submit(text)
        }}
      >
        <textarea
          className={styles.input}
          rows={1}
          value={text}
          placeholder={`예: ${example(request.items)}`}
          disabled={busy}
          autoFocus
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            // 한글 조합 중 Enter는 글자 확정이므로 보내지 않는다
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault()
              void submit(text)
            }
          }}
          aria-label="공개 여부 답변"
        />
        <button type="submit" className={styles.send} disabled={busy || !text.trim()}>
          {busy ? '해석 중…' : '답하기'}
        </button>
      </form>

      {error && <p className={styles.error}>{error}</p>}

      <div className={styles.footer}>
        <span className={styles.footnote}>
          이번 질문에만 적용됩니다. 계속 허용하려면 공개 허용 설정에 적어 주세요.
        </span>
        <span className={styles.quick}>
          {QUICK_ANSWERS.map((a) => (
            <button
              key={a}
              type="button"
              className={styles.quickButton}
              disabled={busy}
              onClick={() => void submit(a)}
            >
              {a}
            </button>
          ))}
        </span>
      </div>
    </section>
  )
}
