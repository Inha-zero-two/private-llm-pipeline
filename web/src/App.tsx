import { useEffect, useRef, useState } from 'react'
import { backend, type RunHandle } from '@/backend'
import type { RunHandlers } from '@/backend/types'
import { Composer } from '@/components/Composer'
import { ConsentPrompt } from '@/components/ConsentPrompt'
import { Conversation } from '@/components/Conversation'
import { Dialog } from '@/components/Dialog'
import dialog from '@/components/Dialog.module.css'
import { PlusIcon, ShieldIcon, SidebarIcon } from '@/components/icons'
import { PrivacySettingsDialog } from '@/components/PrivacySettingsDialog'
import { Sidebar } from '@/components/Sidebar'
import type { Chat, ConsentRequest, PrivacySetting, Turn } from '@/types'
import styles from './App.module.css'

let seq = 0
const nextId = (prefix: string) => `${prefix}${++seq}`

const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err))

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
  /** 서버에 연결하지 못했을 때의 안내 */
  const [loadError, setLoadError] = useState<string | null>(null)
  const runRef = useRef<RunHandle | null>(null)
  /** 질문을 보내는 중(실행 ID를 받기 전)에 다시 보내지 않도록 막는다 */
  const sendingRef = useRef(false)
  /** 서버에서 질문·답변 기록을 불러온 채팅 */
  const loadedRef = useRef(new Set<string>())

  const current = chats.find((c) => c.id === currentChatId) ?? null

  const updateChat = (chatId: string, fn: (chat: Chat) => Chat) =>
    setChats((cs) => cs.map((c) => (c.id === chatId ? fn(c) : c)))

  const handlersFor = (chatId: string, turnId: string): RunHandlers => ({
    update: (fn) =>
      updateChat(chatId, (c) => ({
        ...c,
        turns: c.turns.map((t) => (t.id === turnId ? fn(t) : t)),
      })),
    onConsent: setConsent,
    onFinish: () => {
      runRef.current = null
      setRunningChatId(null)
    },
  })

  // 처음 열 때 서버의 채팅 목록과 공개 허용 설정을 불러온다 (데모는 null)
  useEffect(() => {
    backend
      .listChats()
      .then((list) => {
        if (list) setChats(list.map((c) => ({ ...c, turns: [] })))
      })
      .catch((err) => setLoadError(errorText(err)))
    backend
      .loadSetting()
      .then((s) => s && setSetting(s))
      .catch(() => {})
  }, [])

  const openChat = (chatId: string | null) => {
    setCurrentChatId(chatId)
    if (window.innerWidth <= 768) setSidebarOpen(false)
    if (chatId === null || loadedRef.current.has(chatId)) return
    loadedRef.current.add(chatId)
    backend
      .loadTurns(chatId)
      .then((turns) => {
        if (!turns) return
        updateChat(chatId, (c) => ({ ...c, turns }))
        // 새로고침 등으로 끊긴, 아직 진행 중인 실행이 있으면 스트림에 다시 붙는다
        const running = turns.find((t) => t.running && t.runId !== undefined)
        if (running && !runRef.current) {
          setRunningChatId(chatId)
          runRef.current = backend.attachRun(running.runId!, handlersFor(chatId, running.id))
        }
      })
      .catch((err) => {
        loadedRef.current.delete(chatId)
        setLoadError(errorText(err))
      })
  }

  const send = async (text: string) => {
    if (runRef.current || sendingRef.current) return
    sendingRef.current = true
    let chatId = currentChatId
    try {
      if (chatId === null) {
        chatId = await backend.createChat()
        loadedRef.current.add(chatId) // 새 채팅은 불러올 기록이 없다
      }
    } catch (err) {
      sendingRef.current = false
      setLoadError(errorText(err))
      return
    }
    setLoadError(null)

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
    const id = chatId
    // 첫 질문을 보낼 때 채팅 제목을 정하고, 질문한 채팅을 목록 맨 위로 올린다
    setChats((cs) => {
      const chat = cs.find((c) => c.id === id) ?? { id, title: '', turns: [] }
      return [
        { ...chat, title: chat.title || text.slice(0, 30), turns: [...chat.turns, turn] },
        ...cs.filter((c) => c.id !== id),
      ]
    })
    setCurrentChatId(id)
    setRunningChatId(id)
    const handlers = handlersFor(id, turn.id)
    try {
      runRef.current = await backend.startRun(id, text, setting, handlers)
    } catch (err) {
      handlers.update((t) => ({
        ...t,
        running: false,
        notice: { kind: 'error', text: errorText(err) },
      }))
      handlers.onFinish()
    } finally {
      sendingRef.current = false
    }
  }

  const confirmDelete = async () => {
    const target = deleteTarget
    setDeleteTarget(null)
    if (!target) return
    try {
      await backend.deleteChat(target.id)
    } catch (err) {
      window.alert(errorText(err))
      return
    }
    setChats((cs) => cs.filter((c) => c.id !== target.id))
    loadedRef.current.delete(target.id)
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
      onSend={(text) => void send(text)}
      onStop={() => runRef.current?.stop()}
      autoFocus={!current}
      above={
        running &&
        consent && (
          <ConsentPrompt
            // 새 동의 요청이 오면 입력 상태를 새로 시작한다
            key={consent.consentId ?? 'demo'}
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
              {loadError && <p role="alert">{loadError}</p>}
            </div>
            {composer}
          </div>
        )}
      </main>

      {settingsOpen && (
        <PrivacySettingsDialog
          setting={setting}
          submit={backend.saveSetting}
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
            <button className={dialog.danger} onClick={() => void confirmDelete()}>
              삭제
            </button>
          </>
        }
      />
    </div>
  )
}
