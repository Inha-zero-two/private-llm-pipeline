import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { monitorTurn } from '../components/monitoring'
import { eventElapsed, selectAfterArrival } from './model'
import type { Consent, MonitorState, Query } from './types'
import { useMonitor } from './useMonitor'
import styles from './Monitor.module.css'

export default function Monitor() {
  const { target, state } = useMonitor()
  const key = JSON.stringify(target)
  // 실행이 바뀌면 탭·회차·스크롤 등도 함께 초기화한다.
  return (
    <MonitorRun
      key={key}
      state={state}
      demo={target?.kind === 'demo'}
      hasTarget={target !== null}
    />
  )
}

function MonitorRun({
  state,
  demo,
  hasTarget,
}: {
  state: MonitorState
  demo: boolean
  hasTarget: boolean
}) {
  const { run } = state
  const queries = run?.frontierQueries ?? []
  const count = queries.length
  const [selection, setSelection] = useState({ index: 0, count: 0, unseen: false })
  // 새 회차를 따라가되 이전 회차를 읽는 중에는 선택을 유지한다.
  const selected = selectAfterArrival(selection.index, selection.count, count)
  const arrivedWhileReading = count > selection.count && selection.index < selection.count - 1
  if (count !== selection.count)
    setSelection({
      index: selected,
      count,
      unseen: selection.unseen || arrivedWhileReading,
    })
  const choose = (index: number) =>
    setSelection({ index, count, unseen: index === count - 1 ? false : selection.unseen })
  const query = queries[selected]
  const pending = run?.consents.filter((c) => c.result === null) ?? []
  const [tab, setTab] = useState<'events' | 'consents'>('events')
  const [filter, setFilter] = useState<'all' | 'local' | 'external'>('all')
  const [following, setFollowing] = useState(true)
  const followRef = useRef(true)
  const logRef = useRef<HTMLDivElement>(null)
  const previousTop = useRef(0)
  const programmatic = useRef(false)
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    if (run?.status !== 'running' || state.connection === 'reconnecting') return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [run?.status, state.connection])
  const pauseFollowing = () => {
    followRef.current = false
    setFollowing(false)
  }
  const latest = () => {
    followRef.current = true
    setFollowing(true)
    if (logRef.current) {
      programmatic.current = true
      logRef.current.scrollTop = logRef.current.scrollHeight
      previousTop.current = logRef.current.scrollTop
      requestAnimationFrame(() => {
        programmatic.current = false
      })
    }
  }
  useLayoutEffect(() => {
    const box = logRef.current
    if (!box || !followRef.current) return
    programmatic.current = true
    box.scrollTop = box.scrollHeight
    previousTop.current = box.scrollTop
    const frame = requestAnimationFrame(() => {
      programmatic.current = false
    })
    return () => cancelAnimationFrame(frame)
  }, [state.events, filter, tab])

  const log = run
    ? monitorTurn(
        {
          id: String(run.runId),
          question: state.question,
          running: run.status === 'running',
          step: state.step,
          frontier: [],
          consents: [],
          answer: '',
          endReason: run.endReason,
          errorCode: run.errorCode,
          errorStage: run.errorStage,
          notice: run.notice
            ? {
                kind: run.errorCode ? 'error' : run.endReason === 'stopped' ? 'stopped' : 'limited',
                text: run.notice,
              }
            : null,
        },
        pending.length > 0,
        demo,
      )
    : null
  const successful = queries.filter((q) => q.status === 'ok').length
  const failed = queries.filter((q) => q.status === 'failed').length
  const records = run?.consents.filter((c) => c.result !== null) ?? []
  const events = state.events.filter((e) => filter === 'all' || e.source === filter)
  const at =
    run?.endedAt ??
    (state.connection === 'reconnecting'
      ? state.events.at(-1)?.createdAt
      : new Date(now).toISOString())
  const elapsed = eventElapsed(run?.startedAt, at ?? null)
  const terminal = run?.status === 'finished'
  const connectionLabel = {
    loading: '모니터 연결 중',
    connected: demo ? '데모 연결 정상' : '모니터 연결 정상',
    reconnecting: '연결 끊김 · 재연결 중',
    finished: '마지막 실행 기록 유지',
    missing: '기록을 불러올 수 없음',
  }[state.connection]
  const status = !hasTarget
    ? '실행 대기'
    : !run
      ? demo
        ? '데모 실행 전달 대기'
        : '실행 기록 불러오는 중'
      : log?.status
  const displayStep = terminal ? '실행 종료' : (state.step ?? '시작 대기')

  return (
    <div className={styles.monitor} data-error={!!run?.errorCode}>
      <section className={styles.left} aria-label="파이프라인 모니터">
        <section className={styles.status} aria-label="현재 실행 상태">
          <div className={styles.context}>
            {run
              ? `${state.chatTitle || '채팅'} / ${demo ? 'DEMO' : `RUN #${run.runId}`}`
              : 'PIPELINE MONITOR'}
          </div>
          <div className={styles.statusRow}>
            <div>
              <h1>{run?.status === 'running' && !pending.length ? displayStep : status}</h1>
              <p>
                {run
                  ? pending.length
                    ? '동의 입력은 기존 조작 화면에서 진행합니다.'
                    : terminal
                      ? '다음 요청이 시작될 때 자동 전환됩니다.'
                      : '현재 실행 중인 요청을 표시합니다.'
                  : '기존 화면에서 요청을 시작하면 이곳에 표시됩니다.'}
              </p>
            </div>
            <div className={styles.metrics}>
              <div>
                <strong>{elapsed}</strong>
                <span>실행 경과</span>
              </div>
              <div>
                <strong>{String(count).padStart(2, '0')}</strong>
                <span>외부 질의</span>
              </div>
            </div>
          </div>
        </section>
        <div className={styles.question}>
          <span>현재 질문</span>
          <p title={state.question}>{state.question || '아직 전달받은 질문이 없습니다.'}</p>
        </div>
        <div className={styles.pairNav}>
          <strong>{query ? `외부 질의 ${query.round}회차` : '외부 질의 대기'}</strong>
          <div className={styles.pairControls}>
            {(selection.unseen || arrivedWhileReading) && selected < count - 1 && (
              <button className={styles.newQuery} onClick={() => choose(count - 1)}>
                새 질의 도착 · 최신 보기
              </button>
            )}
            {count > 1 && (
              <button
                aria-label="이전 질의의 요청과 응답"
                disabled={selected === 0}
                onClick={() => choose(selected - 1)}
              >
                ‹
              </button>
            )}
            <span>
              {count ? selected + 1 : 0} / {count}
            </span>
            {count > 1 && (
              <button
                aria-label="다음 질의의 요청과 응답"
                disabled={selected === count - 1}
                onClick={() => choose(selected + 1)}
              >
                ›
              </button>
            )}
          </div>
        </div>
        <div className={styles.exchange}>
          <Payload
            key={`request-${query?.queryId}`}
            query={query}
            kind="request"
            stopped={run?.endReason === 'stopped'}
            terminal={terminal}
          />
          <Payload
            key={`response-${query?.queryId}`}
            query={query}
            kind="response"
            stopped={run?.endReason === 'stopped'}
            terminal={terminal}
          />
        </div>
        <div className={styles.connection} data-offline={state.connection === 'reconnecting'}>
          <span>{hasTarget ? connectionLabel : '실행 대기'}</span>
          <span>
            {state.connectionMessage ??
              (demo
                ? '브라우저 안 데모 · 원본 창을 열어 두세요'
                : '기존 화면에서 전달받은 실행만 표시')}
          </span>
        </div>
      </section>

      <aside className={styles.right} aria-label="실행 로그">
        <div className={styles.panelHead}>
          <h2>실행 로그</h2>
          <span>{run ? (demo ? 'DEMO' : `RUN #${run.runId}`) : '대기'}</span>
        </div>
        <section className={styles.summary} aria-label="실행 상세" tabIndex={0}>
          <p className={styles.summaryQuestion}>
            {state.chatTitle && <span>{state.chatTitle} · </span>}
            {state.question || '아직 실행 기록이 없습니다.'}
          </p>
          <dl className={styles.facts}>
            <Fact label="실행 상태" value={log?.status ?? '실행 대기'} />
            <Fact label="현재 단계" value={run ? displayStep : '—'} />
            <Fact
              label="응답 수신"
              value={`${successful} / ${count}회${failed ? ` · ${failed}회 실패` : ''}`}
            />
            <Fact label="종료 사유" value={run ? (log?.endReason ?? '실행 중') : '—'} />
            <Fact
              label="오류"
              value={run?.errorCode ? (run.notice ?? '상세 미제공') : '기록 없음'}
              wide
              error={!!run?.errorCode}
            />
            <Fact label="오류 코드" value={run?.errorCode ?? '미제공'} error={!!run?.errorCode} />
            <Fact
              label="오류 발생 단계"
              value={run?.errorStage ?? '미제공'}
              error={!!run?.errorCode}
            />
            <Fact
              label="공개 동의 대기"
              value={
                pending.flatMap((c) => c.items.map((i) => `${i.label}: ${i.text}`)).join(', ') ||
                '없음'
              }
              wide
            />
          </dl>
        </section>
        <div className={styles.tabs} role="tablist" aria-label="실행 기록 종류">
          {(['events', 'consents'] as const).map((value) => (
            <button
              key={value}
              role="tab"
              id={`${value}-tab`}
              aria-selected={tab === value}
              aria-controls={`${value}-panel`}
              tabIndex={tab === value ? 0 : -1}
              onClick={() => setTab(value)}
              onKeyDown={(e) => {
                if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) {
                  e.preventDefault()
                  const next =
                    e.key === 'Home'
                      ? 'events'
                      : e.key === 'End'
                        ? 'consents'
                        : tab === 'events'
                          ? 'consents'
                          : 'events'
                  setTab(next)
                  document.getElementById(`${next}-tab`)?.focus()
                }
              }}
            >
              {value === 'events'
                ? `이벤트 ${events.length} / ${state.events.length}`
                : `공개 동의 기록 ${records.length}건`}
            </button>
          ))}
        </div>
        <div className={styles.recordArea}>
          {tab === 'events' ? (
            <>
              <div className={styles.toolbar}>
                <div role="group" aria-label="로그 필터">
                  {(
                    [
                      ['all', '전체'],
                      ['local', '로컬'],
                      ['external', '외부 질의'],
                    ] as const
                  ).map(([value, label]) => (
                    <button
                      key={value}
                      aria-pressed={filter === value}
                      onClick={() => setFilter(value)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <div>
                  <span>{following ? '최신 로그 따라가는 중' : '이전 로그 읽는 중'}</span>
                  {!following && <button onClick={latest}>최신으로</button>}
                </div>
              </div>
              <div
                className={styles.logScroll}
                ref={logRef}
                tabIndex={0}
                role="tabpanel"
                id="events-panel"
                aria-labelledby="events-tab"
                onWheel={(e) => {
                  if (e.deltaY < 0) pauseFollowing()
                }}
                onKeyDown={(e) => {
                  if (['ArrowUp', 'PageUp', 'Home'].includes(e.key)) pauseFollowing()
                }}
                onScroll={(e) => {
                  const top = e.currentTarget.scrollTop
                  if (!programmatic.current && top < previousTop.current - 2) pauseFollowing()
                  previousTop.current = top
                }}
              >
                <div className={styles.logColumns}>
                  <span>경과 시간</span>
                  <span>처리 위치</span>
                  <span>이벤트</span>
                </div>
                {events.length ? (
                  events.map((event) => (
                    <div
                      key={event.id}
                      className={styles.logRow}
                      data-related={query && event.round === query.round}
                      data-error={event.type === 'error' || event.text === '외부 질의 실패'}
                    >
                      <time>{eventElapsed(run?.startedAt, event.createdAt)}</time>
                      <span className={styles.source}>
                        {event.source === 'external' ? 'FRONTIER' : 'LOCAL'}
                      </span>
                      <div>
                        {event.text}
                        {event.round !== undefined && (
                          <button
                            onClick={() => {
                              const index = queries.findIndex((q) => q.round === event.round)
                              if (index >= 0) choose(index)
                            }}
                          >
                            질의 {event.round}
                          </button>
                        )}
                      </div>
                    </div>
                  ))
                ) : (
                  <p className={styles.empty}>
                    {run
                      ? '해당 이벤트가 없습니다.'
                      : '기존 화면에서 요청을 시작하면 로그가 표시됩니다.'}
                  </p>
                )}
              </div>
            </>
          ) : (
            <div
              className={styles.consents}
              role="tabpanel"
              id="consents-panel"
              aria-labelledby="consents-tab"
              tabIndex={0}
            >
              <p className={styles.note}>
                동의 입력은 기존 화면에서 처리합니다. 모니터에서는 기록만 확인합니다.
              </p>
              {records.length ? (
                records.map((c) => <ConsentEntry key={c.consentId} consent={c} />)
              ) : (
                <p className={styles.empty}>공개 동의 기록 없음</p>
              )}
            </div>
          )}
        </div>
        <div className={styles.logFooter}>
          <span>{demo ? '데모 기록' : '저장된 이벤트와 실시간 상태'}</span>
          <span>{terminal ? '실행 종료 · 기록 유지' : status}</span>
        </div>
      </aside>
    </div>
  )
}

function Fact({
  label,
  value,
  wide,
  error,
}: {
  label: string
  value: string
  wide?: boolean
  error?: boolean
}) {
  return (
    <div data-wide={wide} data-error={error}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  )
}

function Payload({
  query,
  kind,
  stopped,
  terminal,
}: {
  query?: Query
  kind: 'request' | 'response'
  stopped?: boolean
  terminal: boolean
}) {
  const response = kind === 'response'
  const status = !query
    ? '호출 대기'
    : !response
      ? '질의 시작 기록'
      : query.status === 'ok'
        ? '수신 완료'
        : stopped
          ? '중지됨'
          : query.status === 'failed'
            ? '수신 실패'
            : terminal
              ? '결과 불확실'
              : '응답 대기'
  const text = !query
    ? '외부 질의가 시작되면 표시됩니다.'
    : !response
      ? query.request
      : (query.answer ??
        (stopped
          ? '실행이 중지되었습니다.'
          : query.status === 'failed'
            ? '이 요청에 대한 응답을 받지 못했습니다. 오른쪽 오류·종료 사유를 확인해 주세요.'
            : terminal
              ? '확인된 응답이 없습니다. 기록된 요청을 표시합니다.'
              : '프론티어 응답을 기다리고 있습니다.'))
  return (
    <section
      className={styles.payloadPanel}
      data-error={response && query?.status === 'failed'}
      aria-label={response ? '받은 응답' : '보낸 요청'}
    >
      <div className={styles.panelHead}>
        <h2>{response ? '받은 응답' : '보낸 요청'}</h2>
        <span>{response ? 'FRONTIER → LOCAL' : 'LOCAL → FRONTIER'}</span>
      </div>
      <div className={styles.meta}>
        <span>
          {query ? `질의 ${query.round} · ${response ? '응답' : '요청'} 본문` : '질의 대기'}
        </span>
        <span>{status}</span>
      </div>
      <div
        className={styles.payload}
        tabIndex={0}
        role="region"
        aria-label={response ? '받은 응답 본문' : '보낸 요청 본문'}
      >
        {text}
      </div>
    </section>
  )
}

function ConsentEntry({ consent }: { consent: Consent }) {
  return (
    <article className={styles.consentEntry}>
      <h3>
        공개 동의 #{consent.consentId} ·{' '}
        {consent.result === 'cancelled' ? '요청 취소' : '응답 완료'}
      </h3>
      {consent.result === 'cancelled' ? (
        <p>요청 취소: {consent.items.map((i) => `${i.label} ${i.text}`).join(', ')}</p>
      ) : (
        <>
          <blockquote>{consent.responseText || '응답 원문 미제공'}</blockquote>
          {consent.items.map((item) => {
            const decision = consent.decisions.find((d) => d.pdId === item.pdId)
            return (
              <div className={styles.decision} key={item.pdId}>
                <span>{item.label}</span>
                <span>{item.text}</span>
                <strong data-allow={decision?.allow}>
                  {decision ? (decision.allow ? '공개 동의' : '공개 거절') : '결정 미제공'}
                </strong>
              </div>
            )
          })}
        </>
      )}
    </article>
  )
}
