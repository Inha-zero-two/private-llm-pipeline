import { useState } from 'react'
import type { SettingReply } from '@/backend/types'
import type { PrivacySetting } from '@/types'
import { Dialog } from './Dialog'
import dialog from './Dialog.module.css'
import styles from './PrivacySettingsDialog.module.css'

type Props = {
  setting: PrivacySetting
  /** 설정문을 해석해 저장한다 (서버 API-10 또는 데모) */
  submit: (text: string) => Promise<SettingReply>
  onSave: (setting: PrivacySetting) => void
  onClose: () => void
}

const PLACEHOLDER = '예: 직무와 경력 기간은 공개해도 됩니다.\n회사명과 연락처는 공개하지 마세요.'

// 개인정보 공개 허용 설정 (설계서 9장 8.3). 열 때마다 새로 그린다
export function PrivacySettingsDialog({ setting, submit, onSave, onClose }: Props) {
  // 지금 설정을 입력칸에 채워 둔다. 저장은 규칙을 통째로 바꾸기 때문
  const [text, setText] = useState(setting.text)
  const [rules, setRules] = useState(setting.rules)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)

  const save = async () => {
    setSaving(true)
    setError('')
    const result = await submit(text)
    setSaving(false)
    if (!result.ok) {
      setError(result.message)
      return
    }
    setRules(result.rules)
    setSaved(true)
    onSave({ text: text.trim(), rules: result.rules })
  }

  return (
    <Dialog
      open
      onClose={onClose}
      title="개인정보 공개 허용 설정"
      description="외부 모델에 보내도 되는 정보와 보내면 안 되는 정보를 자연어로 적어 주세요. 적지 않은 정보는 보내지 않습니다."
      actions={
        saved ? (
          <button className={dialog.primary} onClick={onClose}>
            닫기
          </button>
        ) : (
          <>
            <button className={dialog.button} onClick={onClose}>
              취소
            </button>
            <button className={dialog.primary} onClick={save} disabled={saving}>
              {saving ? '해석 중…' : '설정'}
            </button>
          </>
        )
      }
    >
      <textarea
        className={styles.input}
        value={text}
        placeholder={PLACEHOLDER}
        disabled={saving || saved}
        onChange={(e) => setText(e.target.value)}
        rows={5}
        aria-label="공개 허용 설정문"
      />

      {error && <p className={styles.error}>{error}</p>}

      {saved && <p className={styles.saved}>저장했습니다. 다음 질문부터 적용됩니다.</p>}

      {rules.length > 0 && (
        <div className={styles.rules}>
          <div className={styles.rulesLabel}>{saved ? '해석된 규칙' : '지금 적용 중인 규칙'}</div>
          <ul className={styles.ruleList}>
            {rules.map((r, i) => (
              <li key={i} className={styles.rule} data-action={r.action}>
                <span className={styles.ruleAction}>{r.action === 'allow' ? '허용' : '금지'}</span>
                {r.target}
                {r.scope === 'category' && <span className={styles.ruleScope}>종류</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Dialog>
  )
}
