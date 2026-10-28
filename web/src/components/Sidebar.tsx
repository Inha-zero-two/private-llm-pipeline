import type { ChatSummary } from '@/api'
import { PlusIcon, ShieldIcon, SidebarIcon, TrashIcon } from './icons'
import styles from './Sidebar.module.css'

type Props = {
  open: boolean
  chats: ChatSummary[]
  currentChatId: string | null
  onToggle: () => void
  onNewChat: () => void
  onSelect: (chatId: string) => void
  onDelete: (chat: ChatSummary) => void
  onOpenSettings: () => void
}

export function Sidebar({
  open,
  chats,
  currentChatId,
  onToggle,
  onNewChat,
  onSelect,
  onDelete,
  onOpenSettings,
}: Props) {
  return (
    <>
      <div className={styles.scrim} data-open={open} onClick={onToggle} />
      <aside className={styles.sidebar} data-open={open} aria-label="채팅 목록">
        <div className={styles.header}>
          <span className={styles.brand}>
            <ShieldIcon size={20} />
            제로투
          </span>
          <button className={styles.iconButton} onClick={onToggle} aria-label="사이드바 닫기">
            <SidebarIcon />
          </button>
        </div>

        <button className={styles.newChat} onClick={onNewChat}>
          <PlusIcon />새 채팅
        </button>

        <nav className={styles.list}>
          {chats.length > 0 && <div className={styles.sectionLabel}>채팅</div>}
          {chats.map((chat) => {
            const running = chat.activeRunId !== null
            return (
              <div
                key={chat.chatId}
                className={styles.item}
                data-active={chat.chatId === currentChatId}
              >
                <button
                  className={styles.itemTitle}
                  onClick={() => onSelect(chat.chatId)}
                  title={chat.title}
                >
                  {running && <span className={styles.runningDot} aria-label="답변 생성 중" />}
                  {chat.title}
                </button>
                <button
                  className={styles.deleteButton}
                  onClick={() => onDelete(chat)}
                  disabled={running}
                  aria-label={`${chat.title} 삭제`}
                  title={running ? '답변을 만드는 중에는 삭제할 수 없습니다' : '삭제'}
                >
                  <TrashIcon size={16} />
                </button>
              </div>
            )
          })}
        </nav>

        <div className={styles.footer}>
          <button className={styles.settings} onClick={onOpenSettings}>
            <ShieldIcon />
            공개 허용 설정
          </button>
        </div>
      </aside>
    </>
  )
}
