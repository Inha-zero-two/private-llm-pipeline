import type { LiveRun } from '@/hooks/useLiveRun'
import { CATEGORY_LABEL } from '@/lib/turns'
import { Dialog } from './Dialog'
import dialog from './Dialog.module.css'
import styles from './ConsentDialog.module.css'

type Props = {
  request: NonNullable<LiveRun['consent']> | null
  answering: boolean
  onAnswer: (allow: boolean) => void
}

// 추가 개인정보 공개 요청 (설계서 9장 8.2). 시간 제한이 없고, 거절 / 허용으로만 닫힌다.
export function ConsentDialog({ request, answering, onAnswer }: Props) {
  return (
    <Dialog
      open={request !== null}
      dismissible={false}
      title="추가 개인정보 공개 요청"
      description="더 정확한 답변을 위해 외부 모델에 아래 정보를 보내도 될까요?"
      actions={
        <>
          <button className={dialog.button} disabled={answering} onClick={() => onAnswer(false)}>
            거절
          </button>
          <button className={dialog.primary} disabled={answering} onClick={() => onAnswer(true)}>
            허용
          </button>
        </>
      }
    >
      {request && (
        <>
          <ul className={styles.items}>
            {request.items.map((item) => (
              <li key={item.pdId} className={styles.item}>
                <span className={styles.category} data-category={item.category}>
                  {CATEGORY_LABEL[item.category]}
                </span>
                {item.text}
              </li>
            ))}
          </ul>
          <dl className={styles.details}>
            <dt>목적</dt>
            <dd>{request.purpose}</dd>
            <dt>예상 이점</dt>
            <dd>{request.benefit}</dd>
            <dt>예상 불이익</dt>
            <dd>{request.risk}</dd>
          </dl>
          <p className={styles.footnote}>
            허용은 이번 질문에만 적용됩니다. 계속 허용하려면 공개 허용 설정에 적어 주세요.
          </p>
        </>
      )}
    </Dialog>
  )
}
