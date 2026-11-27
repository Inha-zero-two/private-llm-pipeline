import { useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { ArrowUpIcon, StopIcon } from './icons'
import styles from './Composer.module.css'

type Props = {
  running: boolean
  /** 입력을 막는 이유. 있으면 전송할 수 없다 */
  blockedReason: string | null
  onSend: (text: string) => void
  onStop: () => void
  autoFocus?: boolean
  /** 입력창 바로 위에 붙는 내용 (동의 요청) */
  above?: ReactNode
  /** 보내지 못해 입력창에 되돌릴 글. 그사이 새로 쓴 글이 있으면 덮지 않는다 */
  restore?: { text: string } | null
  /** 입력창 아래의 개인정보 안내. 질문 원문이 어디로 가는지 알린다 */
  hint: string
}

const MAX_HEIGHT = 220

export function Composer({
  running,
  blockedReason,
  onSend,
  onStop,
  autoFocus,
  above,
  restore = null,
  hint,
}: Props) {
  const [text, setText] = useState('')
  const ref = useRef<HTMLTextAreaElement>(null)

  // 내용에 맞춰 높이를 늘리고, 최대 높이를 넘으면 안에서 스크롤한다
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`
  }, [text])

  // 되돌릴 글이 새로 오면 한 번만 넣는다
  const [restored, setRestored] = useState<Props['restore']>(null)
  if (restore !== restored) {
    setRestored(restore)
    if (restore && !text) setText(restore.text)
  }

  const canSend = text.trim().length > 0 && !running && !blockedReason

  const submit = () => {
    if (!canSend) return
    onSend(text.trim())
    setText('')
  }

  return (
    <div className={styles.wrap}>
      {above}
      <form
        className={styles.box}
        onSubmit={(e) => {
          e.preventDefault()
          submit()
        }}
      >
        <textarea
          ref={ref}
          className={styles.input}
          rows={1}
          value={text}
          placeholder={blockedReason ?? '메시지를 입력하세요.'}
          autoFocus={autoFocus}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            // 한글 조합 중 Enter는 글자 확정이므로 전송하지 않는다
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault()
              submit()
            }
          }}
          aria-label="메시지 입력"
        />
        {running ? (
          <button type="button" className={styles.button} onClick={onStop} aria-label="중지">
            <StopIcon size={16} />
          </button>
        ) : (
          <button type="submit" className={styles.button} disabled={!canSend} aria-label="전송">
            <ArrowUpIcon size={18} />
          </button>
        )}
      </form>
      <p className={styles.hint}>{hint}</p>
    </div>
  )
}
