import { useEffect, useRef, type ReactNode } from 'react'
import styles from './Dialog.module.css'

type Props = {
  open: boolean
  title: string
  description?: ReactNode
  onClose: () => void
  children?: ReactNode
  actions: ReactNode
}

export function Dialog({ open, title, description, onClose, children, actions }: Props) {
  const ref = useRef<HTMLDialogElement>(null)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    if (open && !el.open) el.showModal()
    if (!open && el.open) el.close()
  }, [open])

  return (
    <dialog
      ref={ref}
      className={styles.dialog}
      aria-labelledby="dialog-title"
      onCancel={(e) => {
        e.preventDefault()
        onClose()
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div className={styles.body}>
        <h2 id="dialog-title" className={styles.title}>
          {title}
        </h2>
        {description && <p className={styles.description}>{description}</p>}
        {children}
        <div className={styles.actions}>{actions}</div>
      </div>
    </dialog>
  )
}
