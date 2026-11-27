import { useEffect, useRef, useState } from 'react'
import type { CSSProperties } from 'react'
import { ApiError, backend, type RunHandle } from '@/backend'
import type { RunHandlers } from '@/backend/types'
import { Composer } from '@/components/Composer'
import { ConsentPrompt } from '@/components/ConsentPrompt'
import { Conversation } from '@/components/Conversation'
import { Dialog } from '@/components/Dialog'
import dialog from '@/components/Dialog.module.css'
import { PlusIcon, ShieldIcon, SidebarIcon } from '@/components/icons'
import { PrivacySettingsDialog } from '@/components/PrivacySettingsDialog'
import { Sidebar } from '@/components/Sidebar'
import { ResizeHandle } from '@/components/ResizeHandle'
import { usePanelSizes } from '@/layout/usePanelSizes'
import { openMonitor, publishDemo, publishTarget, readTarget } from '@/monitor/bridge'
import { PrivacyStatus } from '@/components/PrivacyStatus'
import { ComparisonPanel } from '@/comparison/ComparisonPanel'
import { useComparison } from '@/comparison/useComparison'
import {
  COMPARISON_HERO,
  COMPARISON_HINT,
  PIPELINE_HERO,
  PIPELINE_HINT,
  sendsRawQuestion,
  sendState,
  turnToShow,
} from '@/comparison/view'
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
  /** 다른 출력이 끝나지 않아 보내지 못한 질문. 보낸 채팅의 입력창에만 되돌린다 */
  const [rejected, setRejected] = useState<{
    chatId: string
    text: string
    error: ApiError
  } | null>(null)
  /** 비교 패널에 보여 줄 질문. null이면 최근 질문 */
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null)
  const { state: comparison, tracker } = useComparison(backend.name === 'remote')
  const panels = usePanelSizes(sidebarOpen, comparison.enabled)
  const runRef = useRef<RunHandle | null>(null)
  /** 질문을 보내는 중(실행 ID를 받기 전)에 다시 보내지 않도록 막는다 */
  const sendingRef = useRef(false)
  /** 서버에서 질문·답변 기록을 불러온 채팅 */
  const loadedRef = useRef(new Set<string>())
  const demoMonitorRef = useRef<{
    chatId: string
    turnId: string
    targetId: string
    startedAt: string
  } | null>(null)
  const [monitorBlocked, setMonitorBlocked] = useState(false)

  useEffect(() => {
    const active = demoMonitorRef.current
    if (backend.name !== 'demo' || !active) return
    const chat = chats.find((c) => c.id === active.chatId)
    const turn = chat?.turns.find((t) => t.id === active.turnId)
    if (chat && turn)
      publishDemo({
        target: { kind: 'demo', chatId: chat.id, id: active.targetId },
        chatTitle: chat.title,
        turn,
        consent,
        startedAt: active.startedAt,
      })
  }, [chats, consent])

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
    setSelectedRunId(null)
    setRejected(null)
    tracker.open(chatId)
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
          publishTarget({ kind: 'remote', chatId, runId: running.runId! })
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
    setRejected(null)
    setSelectedRunId(null)
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
      endReason: null,
      errorCode: null,
      errorStage: null,
    }
    const id = chatId
    if (backend.name === 'demo') {
      const targetId = crypto.randomUUID()
      demoMonitorRef.current = {
        chatId: id,
        turnId: turn.id,
        targetId,
        startedAt: new Date().toISOString(),
      }
      publishTarget({ kind: 'demo', chatId: id, id: targetId })
    }
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
      const run = await backend.startRun(id, text, setting, handlers)
      runRef.current = run
      if (run.runId !== undefined) {
        tracker.started(id, run.runId)
        publishTarget({ kind: 'remote', chatId: id, runId: run.runId })
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // 다른 출력이 끝나지 않았다. 질문을 지우지 않고 입력창에 되돌린다
        updateChat(id, (c) => ({ ...c, turns: c.turns.filter((t) => t.id !== turn.id) }))
        setRejected({ chatId: id, text, error: err })
        handlers.onFinish()
        tracker.sync().catch(() => {})
        return
      }
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
    tracker.forget(target.id)
    if (readTarget()?.chatId === target.id) publishTarget(null)
    if (target.id === currentChatId) openChat(null)
  }

  const running = runningChatId !== null && runningChatId === currentChatId
  const sending = sendState(currentChatId, runningChatId, comparison.running)
  const shownTurn = turnToShow(current?.turns ?? [], selectedRunId)
  // 응답을 기다리는 사이 다른 채팅을 열었으면 그 채팅에는 되돌리지 않는다
  const rejectedHere = rejected?.chatId === currentChatId ? rejected : null
  // 비교 때문에 막힌 안내는 그 비교가 끝나면 지운다
  const showRejected =
    rejectedHere !== null &&
    (rejectedHere.error.code !== 'COMPARISON_RUNNING' || comparison.running !== null)
  const rawToComparison = sendsRawQuestion(comparison, currentChatId)
  const composer = (
    <Composer
      // 채팅을 바꾸면 입력 중인 글을 비운다
      key={currentChatId ?? 'new'}
      running={sending.running}
      blockedReason={sending.blockedReason}
      onSend={(text) => void send(text)}
      // 같은 중지 API가 파이프라인과 비교를 함께 멈춘다. 파이프라인이 끝났으면 비교만 남았다
      onStop={() => (runRef.current ? runRef.current.stop() : tracker.stop())}
      autoFocus={!current}
      restore={rejectedHere}
      hint={rawToComparison ? COMPARISON_HINT : PIPELINE_HINT}
      above={
        <>
          {showRejected && (
            <p className={styles.waiting} role="alert">
              {rejectedHere.error.message}
            </p>
          )}
          {sending.comparing && (
            <p className={styles.waiting} role="status">
              비교 답변을 만드는 중입니다.
            </p>
          )}
          {running && consent && (
            <ConsentPrompt
              // 새 동의 요청이 오면 입력 상태를 새로 시작한다
              key={consent.consentId ?? 'demo'}
              request={consent}
              onAnswer={(answer) => runRef.current!.answerConsent(answer)}
            />
          )}
        </>
      }
    />
  )

  return (
    <div
      className={styles.app}
      style={
        {
          '--sidebar-w': `${panels.sidebar}px`,
          '--comparison-w': `${panels.comparison}px`,
        } as CSSProperties
      }
    >
      <Sidebar
        open={sidebarOpen}
        chats={chats}
        currentChatId={currentChatId}
        runningChatId={runningChatId}
        comparingChatId={comparison.running?.chatId ?? null}
        onToggle={() => setSidebarOpen((v) => !v)}
        onNewChat={() => openChat(null)}
        onSelect={openChat}
        onDelete={setDeleteTarget}
        onOpenSettings={() => setSettingsOpen(true)}
      >
        <ResizeHandle
          label="채팅 목록 너비 조절"
          controls="chat-sidebar"
          side="right"
          width={panels.sidebar}
          min={panels.sidebarMin}
          max={panels.sidebarMax}
          onResize={(width) => panels.resize('sidebar', width)}
          onReset={() => panels.reset('sidebar')}
        />
      </Sidebar>

      <div className={styles.workspace} data-comparison={comparison.enabled}>
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
            <button
              className={styles.monitorButton}
              onClick={() => setMonitorBlocked(!openMonitor())}
            >
              모니터 창 열기
            </button>
          </header>
          {monitorBlocked && (
            <p className={styles.waiting} role="status">
              팝업이 차단되었습니다. 이 사이트의 팝업을 허용한 뒤 다시 열어 주세요.
            </p>
          )}
          <PrivacyStatus
            setting={setting}
            turn={current?.turns.at(-1)}
            pending={running && consent !== null}
            onOpenSettings={() => setSettingsOpen(true)}
          />

          {current ? (
            <>
              <Conversation
                turns={current.turns}
                selectedRunId={comparison.enabled ? shownTurn?.runId : undefined}
                onSelect={comparison.enabled ? setSelectedRunId : undefined}
              />
              {composer}
            </>
          ) : (
            <div className={styles.empty}>
              <div className={styles.hero}>
                <ShieldIcon size={36} />
                <h2>무엇을 도와드릴까요?</h2>
                <p>{rawToComparison ? COMPARISON_HERO : PIPELINE_HERO}</p>
                {loadError && <p role="alert">{loadError}</p>}
              </div>
              {composer}
            </div>
          )}
        </main>
        {comparison.enabled && (
          <ComparisonPanel
            resizeHandle={
              <ResizeHandle
                label="모델 비교 패널 너비 조절"
                controls="model-comparison"
                side="left"
                width={panels.comparison}
                min={panels.comparisonMin}
                max={panels.comparisonMax}
                onResize={(width) => panels.resize('comparison', width)}
                onReset={() => panels.reset('comparison')}
              />
            }
            state={comparison}
            chatId={currentChatId}
            turn={shownTurn}
            canRetry={runningChatId === null && comparison.running === null}
            onRetry={async (runId, kind) => {
              if (currentChatId !== null) await tracker.retry(currentChatId, runId, kind)
            }}
          />
        )}
      </div>

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
