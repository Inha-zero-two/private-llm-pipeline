import { useLayoutEffect, useRef } from 'react'
import type { Turn as TurnData } from '@/lib/turns'
import { Turn } from './Turn'
import styles from './Conversation.module.css'

// 바닥에서 이 정도 안쪽이면 "맨 아래를 보고 있다"고 본다
const STICK_THRESHOLD = 80

export function Conversation({ turns }: { turns: TurnData[] }) {
  const ref = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const prevCount = useRef(0)

  // 맨 아래를 보고 있을 때만 새 내용을 따라 내려간다. 위로 올려 읽는 중이면 그대로 둔다.
  // 새 질문이 생기면 다시 맨 아래로 간다
  useLayoutEffect(() => {
    const el = ref.current
    if (turns.length > prevCount.current) stick.current = true
    prevCount.current = turns.length
    if (el && stick.current) el.scrollTop = el.scrollHeight
  }, [turns])

  return (
    <div
      ref={ref}
      className={styles.scroll}
      onScroll={(e) => {
        const el = e.currentTarget
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_THRESHOLD
      }}
    >
      <div className={styles.inner}>
        {turns.map((t) => (
          <Turn key={t.key} turn={t} />
        ))}
      </div>
    </div>
  )
}
