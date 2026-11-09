import { useRef, useState } from 'react'
import { Composer } from '@/components/Composer'
import { ConsentPrompt } from '@/components/ConsentPrompt'
import { Conversation } from '@/components/Conversation'
import { Dialog } from '@/components/Dialog'
import dialog from '@/components/Dialog.module.css'
import { PlusIcon, ShieldIcon, SidebarIcon } from '@/components/icons'
import { PrivacySettingsDialog } from '@/components/PrivacySettingsDialog'
import { Sidebar } from '@/components/Sidebar'
import { runDemo, type DemoRun } from '@/demo/run'
import type { Chat, ConsentRequest, PrivacySetting, Turn } from '@/types'
import styles from './App.module.css'

let seq = 0
const nextId = (prefix: string) => `${prefix}${++seq}`

export default function App() {
  const [chats, setChats] = useState<Chat[]>([])
  const [currentChatId, setCurrentChatId] = useState<string | null>(null)
  /** 답변을 만드는 중인 채팅. 한 번에 하나만 실행한다 */
  const [runningChatId, setRunningChatId] = useState<string | null>(null)
  const [consent, setConsent] = useState<ConsentRequest | null>(null)
  /** 공개 허용 설정. 앱 전체에 하나다 (설계서 4.2) */
  const [setting, setSetting] = useState<PrivacySetting>({ text: '', rules: [] })
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth > 768)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<Chat | null>(null)
  const runRef = useRef<DemoRun | null>(null)

  const current = chats.find((c) => c.id === currentChatId) ?? null

  const openChat = (chatId: string | null) => {
    setCurrentChatId(chatId)
    if (window.innerWidth <= 768) setSidebarOpen(false)
  }

  const updateChat = (chatId: string, fn: (chat: Chat) => Chat) =>
    setChats((cs) => cs.map((c) => (c.id === chatId ? fn(c) : c)))

  const send = (text: string) => {
    if (runRef.current) return
    const chatId = currentChatId ?? nextId('c')
    const turn: Turn = {
      id: nextId('t'),
      question: text,
      running: true,
      step: null,
      frontier: [],
      consents: [],
      answer: '',
      notice: null,
    }
    // 첫 질문을 보낼 때 채팅을 만들고, 질문한 채팅을 목록 맨 위로 올린다
    setChats((cs) => {
      const chat = cs.find((c) => c.id === chatId) ?? {
        id: chatId,
        title: text.slice(0, 30),
        turns: [],
      }
      return [{ ...chat, turns: [...chat.turns, turn] }, ...cs.filter((c) => c.id !== chatId)]
    })
    setCurrentChatId(chatId)
    setRunningChatId(chatId)
    runRef.current = runDemo(text, setting.rules, {
      update: (fn) =>
        updateChat(chatId, (c) => ({
          ...c,
          turns: c.turns.map((t) => (t.id === turn.id ? fn(t) : t)),
        })),
      onConsent: setConsent,
      onFinish: () => {
        runRef.current = null
        setRunningChatId(null)
      },
    })
  }

  const confirmDelete = () => {
    const target = deleteTarget
    setDeleteTarget(null)
    if (!target) return
    setChats((cs) => cs.filter((c) => c.id !== target.id))
    if (target.id === currentChatId) openChat(null)
  }

  const running = runningChatId !== null && runningChatId === currentChatId
  const blockedReason =
    runningChatId !== null && runningChatId !== currentChatId
      ? '다른 채팅에서 답변을 만드는 중입니다.'
      : null
  const composer = (
    <Composer
      // 채팅을 바꾸면 입력 중인 글을 비운다
      key={currentChatId ?? 'new'}
      running={running}
      blockedReason={blockedReason}
      onSend={send}
      onStop={() => runRef.current?.stop()}
      autoFocus={!current}
      above={
        running &&
        consent && (
          <ConsentPrompt
            request={consent}
            onAnswer={(answer) => runRef.current!.answerConsent(answer)}
          />
        )
      }
    />
  )

  return (
    <div className={styles.app}>
      <Sidebar
        open={sidebarOpen}
        chats={chats}
        currentChatId={currentChatId}
        runningChatId={runningChatId}
        onToggle={() => setSidebarOpen((v) => !v)}
        onNewChat={() => openChat(null)}
        onSelect={openChat}
        onDelete={setDeleteTarget}
        onOpenSettings={() => setSettingsOpen(true)}
      />

      <main className={styles.main}>
        <header className={styles.topbar}>
          {!sidebarOpen && (
            <>
              <button
                className={styles.iconButton}
                onClick={() => setSidebarOpen(true)}
                aria-label="사이드바 열기"
              >
                <SidebarIcon />
              </button>
              <button
                className={styles.iconButton}
                onClick={() => openChat(null)}
                aria-label="새 채팅"
              >
                <PlusIcon />
              </button>
            </>
          )}
          <h1 className={styles.title}>{current?.title ?? ''}</h1>
        </header>

        {current ? (
          <>
            <Conversation turns={current.turns} />
            {composer}
          </>
        ) : (
          <div className={styles.empty}>
            <div className={styles.hero}>
              <ShieldIcon size={36} />
              <h2>무엇을 도와드릴까요?</h2>
              <p>개인정보는 이 PC 안에서 지키고, 필요한 도움만 외부 모델에 물어봅니다.</p>
            </div>
            {composer}
          </div>
        )}
      </main>

      {settingsOpen && (
        <PrivacySettingsDialog
          setting={setting}
          onSave={setSetting}
          onClose={() => setSettingsOpen(false)}
        />
      )}

      <Dialog
        open={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title="채팅을 삭제할까요?"
        description={
          <>
            <strong>{deleteTarget?.title}</strong> 채팅의 메시지와 기록이 모두 지워집니다. 공개 허용
            설정은 그대로 남습니다.
          </>
        }
        actions={
          <>
            <button className={dialog.button} onClick={() => setDeleteTarget(null)}>
              취소
            </button>
            <button className={dialog.danger} onClick={confirmDelete}>
              삭제
            </button>
          </>
        }
      />
    </div>
  )
}
