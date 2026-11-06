import { useEffect, useRef, useState } from 'react'
import { interpretSetting } from '@/demo/rules'
import type { PrivacySetting } from '@/types'
import dialog from './Dialog.module.css'
import { ShieldIcon } from './icons'
import styles from './PrivacySettings.module.css'

type Props = {
  setting: PrivacySetting
  onSave: (setting: PrivacySetting) => void
}

const PLACEHOLDER = '예: 직무와 경력 기간은 공개해도 됩니다.\n회사명과 연락처는 공개하지 마세요.'

// 개인정보 공개 허용 설정 (설계서 9장 8.3). 채팅마다 따로 두므로 입력창 도구 줄에 붙인다.
export function PrivacySettings({ setting, onSave }: Props) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  // 바깥을 누르거나 Esc를 누르면 닫는다
  useEffect(() => {
    if (!open) return
    const onPointer = (e: PointerEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('pointerdown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const allow = setting.rules.filter((r) => r.action === 'allow').length
  const deny = setting.rules.length - allow

  return (
    <div ref={ref}>
      <button
        type="button"
        className={styles.chip}
        data-open={open}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <ShieldIcon size={15} />
        공개 허용 설정
        <span className={styles.summary}>
          {setting.rules.length === 0 ? '모두 비공개' : `허용 ${allow} · 금지 ${deny}`}
        </span>
      </button>

      {open && (
        <Panel
          setting={setting}
          onSave={(s) => {
            onSave(s)
            setOpen(false)
          }}
          onClose={() => setOpen(false)}
        />
      )}
    </div>
  )
}

function Panel({
  setting,
  onSave,
  onClose,
}: Props & {
  onClose: () => void
}) {
  // 지금 설정을 입력칸에 채워 둔다. 저장은 규칙을 통째로 바꾸기 때문
  const [text, setText] = useState(setting.text)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const save = async () => {
    setSaving(true)
    setError('')
    const result = await interpretSetting(text)
    setSaving(false)
    if (!result.ok) {
      setError(result.message)
      return
    }
    onSave({ text: text.trim(), rules: result.rules })
  }

  return (
    <div className={styles.panel} role="dialog" aria-label="이 채팅의 공개 허용 설정">
      <div className={styles.title}>이 채팅의 공개 허용 설정</div>
      <p className={styles.description}>
        외부 모델에 보내도 되는 정보와 보내면 안 되는 정보를 자연어로 적어 주세요. 적지 않은 정보는
        보내지 않습니다.
      </p>

      <textarea
        className={styles.input}
        value={text}
        placeholder={PLACEHOLDER}
        disabled={saving}
        autoFocus
        onChange={(e) => setText(e.target.value)}
        rows={4}
        aria-label="공개 허용 설정문"
      />

      {error && <p className={styles.error}>{error}</p>}

      {setting.rules.length > 0 && (
        <div className={styles.rules}>
          <div className={styles.rulesLabel}>지금 적용 중인 규칙</div>
          <ul className={styles.ruleList}>
            {setting.rules.map((r, i) => (
              <li key={i} className={styles.rule} data-action={r.action}>
                <span className={styles.ruleAction}>{r.action === 'allow' ? '허용' : '금지'}</span>
                {r.target}
                {r.scope === 'category' && <span className={styles.ruleScope}>종류</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className={styles.actions}>
        <button type="button" className={dialog.button} onClick={onClose}>
          취소
        </button>
        <button type="button" className={dialog.primary} onClick={save} disabled={saving}>
          {saving ? '해석 중…' : '저장'}
        </button>
      </div>
    </div>
  )
}
