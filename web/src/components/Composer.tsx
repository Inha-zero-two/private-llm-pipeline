import { useLayoutEffect, useRef, useState } from 'react'
import { ArrowUpIcon, StopIcon } from './icons'
import styles from './Composer.module.css'

type Props = {
  running: boolean
  /** 입력을 막는 이유. 있으면 전송할 수 없다 */
  blockedReason: string | null
  onSend: (text: string) => Promise<boolean>
  onStop: () => void
  autoFocus?: boolean
}

const MAX_HEIGHT = 220

export function Composer({ running, blockedReason, onSend, onStop, autoFocus }: Props) {
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const ref = useRef<HTMLTextAreaElement>(null)

  // 내용에 맞춰 높이를 늘리고, 최대 높이를 넘으면 안에서 스크롤한다
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`
  }, [text])

  const canSend = text.trim().length > 0 && !running && !sending && !blockedReason

  const submit = async () => {
    if (!canSend) return
    const value = text.trim()
    setSending(true)
    setText('')
    const ok = await onSend(value)
    if (!ok) setText(value) // 실패하면 입력을 되돌린다
    setSending(false)
    ref.current?.focus()
  }

  return (
    <div className={styles.wrap}>
      <form
        className={styles.box}
        onSubmit={(e) => {
          e.preventDefault()
          void submit()
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
              void submit()
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
      <p className={styles.hint}>
        질문 원문과 개인정보는 이 PC의 로컬 모델만 봅니다. 외부 모델에는 검사를 통과한 요청문만
        보냅니다.
      </p>
    </div>
  )
}
