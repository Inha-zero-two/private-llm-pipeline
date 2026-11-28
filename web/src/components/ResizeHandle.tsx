import { useEffect, useRef, useState } from 'react'
import styles from './ResizeHandle.module.css'

type Props = {
  label: string
  controls: string
  side: 'left' | 'right'
  width: number
  min: number
  max: number
  onResize: (width: number) => void
  onReset: () => void
}

/** 마우스·펜 드래그와 키보드로 같은 경계를 이동한다. */
export function ResizeHandle({ label, controls, side, width, min, max, onResize, onReset }: Props) {
  const start = useRef<{ pointer: number; x: number; width: number } | null>(null)
  const [dragging, setDragging] = useState(false)
  const direction = side === 'right' ? 1 : -1
  useEffect(() => {
    if (!dragging) return
    const body = document.body
    const cursor = body.style.cursor,
      userSelect = body.style.userSelect
    body.style.cursor = 'col-resize'
    body.style.userSelect = 'none'
    return () => {
      body.style.cursor = cursor
      body.style.userSelect = userSelect
    }
  }, [dragging])

  return (
    <div
      className={styles.handle}
      data-side={side}
      data-dragging={dragging}
      role="separator"
      aria-label={label}
      aria-controls={controls}
      aria-orientation="vertical"
      aria-valuemin={Math.round(min)}
      aria-valuemax={Math.round(max)}
      aria-valuenow={Math.round(width)}
      aria-valuetext={`${Math.round(width)}픽셀`}
      tabIndex={0}
      title="드래그해서 너비 조절 · 좌우 방향키로 조절 · 두 번 클릭하면 기본 너비"
      onDoubleClick={onReset}
      onKeyDown={(event) => {
        let next: number
        if (event.key === 'ArrowLeft') next = width - (event.shiftKey ? 40 : 10) * direction
        else if (event.key === 'ArrowRight') next = width + (event.shiftKey ? 40 : 10) * direction
        else if (event.key === 'Home') next = min
        else if (event.key === 'End') next = max
        else if (event.key === 'Enter') {
          event.preventDefault()
          onReset()
          return
        } else return
        event.preventDefault()
        onResize(next)
      }}
      onPointerDown={(event) => {
        if (event.button !== 0) return
        event.preventDefault()
        event.currentTarget.focus({ preventScroll: true })
        event.currentTarget.setPointerCapture(event.pointerId)
        start.current = { pointer: event.pointerId, x: event.clientX, width }
        setDragging(true)
      }}
      onPointerMove={(event) => {
        const initial = start.current
        if (initial?.pointer === event.pointerId)
          onResize(initial.width + (event.clientX - initial.x) * direction)
      }}
      onPointerUp={(event) => {
        if (start.current?.pointer !== event.pointerId) return
        start.current = null
        setDragging(false)
        if (event.currentTarget.hasPointerCapture(event.pointerId))
          event.currentTarget.releasePointerCapture(event.pointerId)
      }}
      onPointerCancel={() => {
        start.current = null
        setDragging(false)
      }}
      onLostPointerCapture={() => {
        start.current = null
        setDragging(false)
      }}
    >
      <span />
    </div>
  )
}
