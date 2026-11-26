import type { Consent, DemoSnapshot, MonitorState } from './types.ts'
import { emptyMonitor } from './model.ts'

/** 서버 없는 데모도 같은 모니터 화면을 쓰되 실제 관측한 단계만 기록한다. */
export function demoState(previous: MonitorState, snapshot: DemoSnapshot): MonitorState {
  const { turn, consent, startedAt } = snapshot
  const at = new Date().toISOString()
  const queries = turn.frontier.map((q) => ({
    queryId: q.round,
    round: q.round,
    request: q.request,
    answer: q.answer,
    status: 'ok' as const,
  }))
  const consents: Consent[] = turn.consents.map((c, i) => ({
    consentId: i + 1,
    result: c.kind === 'answered' ? ('answered' as const) : ('cancelled' as const),
    responseText: c.kind === 'answered' ? c.answer : null,
    items:
      c.kind === 'cancelled'
        ? c.items.map((item) => ({ ...item, pdId: item.id }))
        : c.decisions.map((d) => ({ ...d.item, pdId: d.item.id })),
    decisions:
      c.kind === 'answered' ? c.decisions.map((d) => ({ pdId: d.item.id, allow: d.allow })) : [],
  }))
  if (consent)
    consents.push({
      consentId: consents.length + 1,
      result: null,
      responseText: null,
      items: consent.items.map((item) => ({ ...item, pdId: item.id })),
      decisions: [],
    })
  const events = [...previous.events]
  const append = (type: string, text: string, round?: number) =>
    events.push({
      id: events.length + 1,
      type,
      text,
      source: round ? 'external' : 'local',
      createdAt: at,
      round,
    })
  if (turn.step && turn.step !== previous.step) append('step', turn.step)
  for (const q of queries)
    if (!previous.run?.frontierQueries.some((p) => p.round === q.round))
      append('frontier_result', '외부 응답 수신', q.round)
  if (turn.consents.length > (previous.run?.consents.filter((c) => c.result).length ?? 0))
    append('consent_result', '공개 동의 응답 기록')
  if (!turn.running && previous.run?.status !== 'finished')
    append(turn.notice?.kind === 'error' ? 'error' : 'done', turn.notice?.text ?? '실행 종료')
  return {
    ...emptyMonitor(),
    chatTitle: snapshot.chatTitle,
    question: turn.question,
    step: turn.step,
    events,
    connection: turn.running ? 'connected' : 'finished',
    demoError: turn.notice?.kind === 'error',
    run: {
      runId: 0,
      chatId: 0,
      userMessageId: 0,
      status: turn.running ? 'running' : 'finished',
      endReason:
        turn.endReason ??
        (turn.running ? null : turn.notice?.kind === 'stopped' ? 'stopped' : null),
      errorCode: turn.errorCode ?? null,
      errorStage: turn.errorStage ?? null,
      notice: turn.notice?.text ?? null,
      startedAt,
      endedAt: turn.running ? null : (previous.run?.endedAt ?? at),
      frontierQueries: queries,
      consents,
    },
  }
}
