import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError, type ChatDetail, type ChatSummary } from '@/api'
import { Composer } from '@/components/Composer'
import { ConsentDialog } from '@/components/ConsentDialog'
import { Conversation } from '@/components/Conversation'
import { Dialog } from '@/components/Dialog'
import dialog from '@/components/Dialog.module.css'
import { PlusIcon, ShieldIcon, SidebarIcon } from '@/components/icons'
import { PrivacySettingsDialog } from '@/components/PrivacySettingsDialog'
import { ServerBanner } from '@/components/ServerBanner'
import { Sidebar } from '@/components/Sidebar'
import { useLiveRun } from '@/hooks/useLiveRun'
import { serverBlockReason, useServerStatus } from '@/hooks/useServerStatus'
import { buildTurns, type Turn } from '@/lib/turns'
import styles from './App.module.css'

const errorText = (e: unknown) =>
  e instanceof ApiError ? e.message : '요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.'

export default function App() {
  const [chats, setChats] = useState<ChatSummary[]>([])
  const [currentChatId, setCurrentChatId] = useState<string | null>(null)
  const [detail, setDetail] = useState<ChatDetail | null>(null)
  /** 보내는 중인 질문. 서버가 받기 전부터 화면에 보여준다 */
  const [pendingQuestion, setPendingQuestion] = useState<string | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth > 768)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<ChatSummary | null>(null)
  const [consentAnswering, setConsentAnswering] = useState(false)
  const [toast, setToast] = useState<string | null>(null)
  const currentChatRef = useRef<string | null>(null)

  /** 서버 상태를 다시 확인하는 함수. useServerStatus가 아래에서 채운다 */
  const recheckRef = useRef<() => void>(() => {})

  const showError = useCallback((e: unknown) => {
    // 연결 문제는 알림 대신 상단 배너로 보여준다
    if (e instanceof ApiError && (e.code === 'NETWORK' || e.code === 'TIMEOUT')) {
      recheckRef.current()
      return
    }
    setToast(errorText(e))
  }, [])

  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 4000)
    return () => clearTimeout(t)
  }, [toast])

  const refreshChats = useCallback(async () => {
    try {
      setChats(await api.listChats())
    } catch (e) {
      showError(e)
    }
  }, [showError])

  const loadChat = useCallback(
    async (chatId: string) => {
      try {
        const d = await api.getChat(chatId)
        if (currentChatRef.current === chatId) setDetail(d)
      } catch (e) {
        showError(e)
      }
    },
    [showError],
  )

  useEffect(() => {
    api.listChats().then(setChats).catch(showError)
  }, [showError])

  // 앱 서버는 웹과 다른 곳에 있다. 끊겼다가 돌아오면 목록과 보던 채팅을 다시 읽는다
  const server = useServerStatus(() => {
    void refreshChats()
    if (currentChatRef.current) void loadChat(currentChatRef.current)
  })
  const { recheck } = server
  useEffect(() => {
    recheckRef.current = () => void recheck()
  }, [recheck])

  const openChat = (chatId: string | null) => {
    currentChatRef.current = chatId
    setCurrentChatId(chatId)
    setDetail(null)
    if (chatId) void loadChat(chatId)
    if (window.innerWidth <= 768) setSidebarOpen(false)
  }

  // 실행은 앱 전체에서 하나뿐이다 (A-07). 다른 채팅을 보고 있어도 끝나는 것을 알아야 목록을 갱신할 수 있다
  const globalRunId = chats.find((c) => c.activeRunId)?.activeRunId ?? null
  const runId = detail?.activeRunId ?? globalRunId
  const live = useLiveRun(runId, () => {
    void refreshChats()
    if (currentChatRef.current) void loadChat(currentChatRef.current)
  })

  const turns = useMemo<Turn[]>(() => {
    const list = detail ? buildTurns(detail, live) : []
    if (pendingQuestion) {
      list.push({
        key: 'pending',
        question: pendingQuestion,
        running: true,
        step: '질문을 보내는 중',
        frontier: [],
        consentResults: [],
        answer: '',
        notice: null,
      })
    }
    return list
  }, [detail, live, pendingQuestion])

  const currentRunning = pendingQuestion !== null || turns.some((t) => t.running)
  const blockedReason =
    serverBlockReason(server.status) ??
    (globalRunId && globalRunId !== detail?.activeRunId
      ? '다른 채팅에서 답변을 만드는 중입니다.'
      : null)

  const send = async (text: string) => {
    let chatId = currentChatId
    let created = false
    setPendingQuestion(text)
    try {
      if (!chatId) {
        // 상용 서비스처럼 첫 질문을 보낼 때 채팅을 만든다
        chatId = (await api.createChat()).chatId
        created = true
        currentChatRef.current = chatId
        setCurrentChatId(chatId)
      }
      await api.sendMessage(chatId, text)
      await Promise.all([loadChat(chatId), refreshChats()])
      return true
    } catch (e) {
      showError(e)
      if (created && chatId) {
        await api.deleteChat(chatId).catch(() => {})
        openChat(null)
      }
      return false
    } finally {
      setPendingQuestion(null)
    }
  }

  const stop = () => {
    if (runId) api.stopRun(runId).catch(showError)
  }

  const answerConsent = async (allow: boolean) => {
    if (!live) return
    setConsentAnswering(true)
    try {
      await api.answerConsent(live.runId, allow)
    } catch (e) {
      showError(e)
    } finally {
      setConsentAnswering(false)
    }
  }

  const confirmDelete = async () => {
    const target = deleteTarget
    setDeleteTarget(null)
    if (!target) return
    try {
      await api.deleteChat(target.chatId)
      if (target.chatId === currentChatId) openChat(null)
      await refreshChats()
    } catch (e) {
      showError(e)
    }
  }

  const empty = turns.length === 0 && (currentChatId === null || detail !== null)

  return (
    <div className={styles.app}>
      <Sidebar
        open={sidebarOpen}
        chats={chats}
        currentChatId={currentChatId}
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
          <h1 className={styles.title}>{detail?.title ?? ''}</h1>
        </header>

        <ServerBanner status={server.status} onRetry={() => void recheck()} />

        {empty ? (
          <div className={styles.empty}>
            <div className={styles.hero}>
              <ShieldIcon size={36} />
              <h2>무엇을 도와드릴까요?</h2>
              <p>개인정보는 이 PC 안에서 지키고, 필요한 도움만 외부 모델에 물어봅니다.</p>
            </div>
            <Composer
              running={currentRunning}
              blockedReason={blockedReason}
              onSend={send}
              onStop={stop}
              autoFocus
            />
          </div>
        ) : (
          <>
            <Conversation turns={turns} />
            <Composer
              running={currentRunning}
              blockedReason={blockedReason}
              onSend={send}
              onStop={stop}
            />
          </>
        )}
      </main>

      <ConsentDialog
        request={live?.consent ?? null}
        answering={consentAnswering}
        onAnswer={answerConsent}
      />

      {settingsOpen && <PrivacySettingsDialog onClose={() => setSettingsOpen(false)} />}

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

      {toast && (
        <div className={styles.toast} role="alert">
          {toast}
        </div>
      )}
    </div>
  )
}
