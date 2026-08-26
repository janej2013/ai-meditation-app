/**
 * The companion conversation as state. Names follow the design prototype's
 * state (thread, toolLine, streaming, proposal, briefOpen, turn, compError,
 * starting, crisis) so the screens read like their artboards; what the
 * prototype simulated -- typing, scripted replies, keyword crisis detection --
 * is here the runner's real event stream.
 *
 * Money never moves in this hook except in `start()`, which calls the one
 * route that can spend a credit, after the listener chose to.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  abandonSession,
  confirmSession,
  createSession,
  getMemory,
  getSession,
  sendTurn,
  type PendingProposal,
  type TurnEvent,
} from '../api/agent'
import { DEFAULT_BGM_TRACK, bgmUrl, mixer } from '../audio/mixer'
import { ApiError, NotSignedInError, getAccount } from '../api/client'
import { isCrisisReply } from './detectCrisis'

/** Mirrors backend/agent/budget.py MAX_TURNS; the runner also reports turns_left. */
export const MAX_TURNS = 12
/** The header counter appears once this few replies remain. */
export const SHOW_TURNS_LEFT_AT = 3

const STORAGE_KEY = 'drift:companion-session'

export type Role = 'user' | 'companion'
export interface Message {
  role: Role
  text: string
  crisis?: boolean
}

export type CompanionError = 'model' | 'exhausted' | 'nocredit' | 'start' | null
export type Gate = 'open' | 'plan_required' | 'quota' | 'signin'

/** What the model says it is doing, per tool, while the reply is prepared. */
export const TOOL_LINES: Record<string, string> = {
  get_session_history: 'Looking back at your earlier sessions…',
  save_user_insight: "Noted — I'll remember that.",
  finalize_meditation_brief: 'Putting a meditation together…',
}
const DEFAULT_TOOL_LINE = 'Working on it…'

export interface Proposal extends PendingProposal {
  summary: string
}

const SUMMARY_CHARS = 90

function summarise(brief: string): string {
  const text = brief.trim().replace(/\s+/g, ' ')
  if (text.length <= SUMMARY_CHARS) return text
  const cut = text.lastIndexOf(' ', SUMMARY_CHARS)
  return text.slice(0, cut > 40 ? cut : SUMMARY_CHARS) + '…'
}

export interface CompanionState {
  gate: Gate
  thread: Message[]
  draft: string
  toolLine: string
  streaming: boolean
  streamText: string
  proposal: Proposal | null
  briefOpen: boolean
  changing: boolean
  turn: number
  turnsLeft: number
  compError: CompanionError
  starting: boolean
  crisis: boolean
  credits: number | null
  insightsCount: number
  busy: boolean
}

export function useCompanion(onStarted: (jobId: string, durationMinutes: number) => void) {
  const [gate, setGate] = useState<Gate>('open')
  const [sessionId, setSessionId] = useState<string | null>(() => {
    try {
      return sessionStorage.getItem(STORAGE_KEY)
    } catch {
      return null
    }
  })
  const [thread, setThread] = useState<Message[]>([])
  const [draft, setDraft] = useState('')
  const [toolLine, setToolLine] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [streamText, setStreamText] = useState('')
  const [proposal, setProposal] = useState<Proposal | null>(null)
  const [briefOpen, setBriefOpen] = useState(false)
  // "Change something" was tapped: the card stays (Start is still there)
  // but says it is listening, until the next message replaces the proposal.
  const [changing, setChanging] = useState(false)
  const [turn, setTurn] = useState(0)
  const [turnsLeft, setTurnsLeft] = useState(MAX_TURNS)
  const [compError, setCompError] = useState<CompanionError>(null)
  const [starting, setStarting] = useState(false)
  const [crisis, setCrisis] = useState(false)
  const [credits, setCredits] = useState<number | null>(null)
  const [insightsCount, setInsightsCount] = useState(0)
  const [busy, setBusy] = useState(false)
  const lastSent = useRef<string | null>(null)
  const abort = useRef<AbortController | null>(null)

  const remember = (id: string | null) => {
    setSessionId(id)
    try {
      if (id) sessionStorage.setItem(STORAGE_KEY, id)
      else sessionStorage.removeItem(STORAGE_KEY)
    } catch {
      /* storage may be unavailable; the session simply does not survive a reload */
    }
  }

  // Credits for the proposal card's "Uses 1 credit · N left", read when a
  // proposal arrives rather than on open: fresher, and not a second read of
  // the account behind the shell's own on a reload. A failure is cosmetic.
  const readCredits = () => {
    getAccount()
      .then((a) => setCredits(a.available))
      .catch(() => setCredits(null))
  }

  // Whether it remembers anything, for the empty state's line. A GET, so it
  // costs no session quota; any refusal (signed out, not Pro) just means no
  // line -- the gate shows itself on the first send.
  useEffect(() => {
    getMemory()
      .then((m) => setInsightsCount(m.insights.length))
      .catch(() => undefined)
  }, [])

  // A reload resumes the conversation the store still holds.
  useEffect(() => {
    if (!sessionId) return
    let cancelled = false
    getSession(sessionId)
      .then((t) => {
        if (cancelled) return
        if (t.status !== 'ACTIVE') {
          remember(null)
          return
        }
        setThread(
          t.turns.flatMap((x) => [
            { role: 'user' as const, text: x.user_text },
            ...(x.assistant_text
              ? [
                  {
                    role: 'companion' as const,
                    text: x.assistant_text,
                    crisis: isCrisisReply(x.assistant_text),
                  },
                ]
              : []),
          ]),
        )
        setTurn(t.turn)
        setTurnsLeft(Math.max(MAX_TURNS - t.turn, 0))
        setProposal(t.pending ? { ...t.pending, summary: summarise(t.pending.brief) } : null)
        if (t.pending) readCredits()
      })
      .catch(() => remember(null))
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- resume once, for the stored id
  }, [])

  useEffect(() => () => abort.current?.abort(), [])

  const ensureSession = async (): Promise<string | null> => {
    if (sessionId) return sessionId
    try {
      const created = await createSession()
      remember(created.session_id)
      setInsightsCount(created.insights_count)
      return created.session_id
    } catch (e) {
      if (e instanceof NotSignedInError) setGate('signin')
      else if (e instanceof ApiError && e.detail === 'plan_required') setGate('plan_required')
      else if (e instanceof ApiError && e.detail === 'quota_exhausted') setGate('quota')
      else setCompError('model')
      return null
    }
  }

  const respond = async (id: string, text: string) => {
    setBusy(true)
    setToolLine('')
    setStreaming(false)
    setStreamText('')
    let streamed = ''
    let proposed: number | null = null
    let finished: TurnEvent['data'] | null = null
    abort.current = new AbortController()
    const handle = (event: TurnEvent) => {
      switch (event.event) {
        case 'tool':
          setToolLine(TOOL_LINES[event.data.name] ?? DEFAULT_TOOL_LINE)
          break
        case 'delta':
          streamed += event.data.text
          setToolLine('')
          setStreaming(true)
          setStreamText(streamed)
          break
        case 'proposal':
          proposed = event.data.duration_minutes
          break
        case 'done':
          finished = event.data
          break
        case 'error':
          setCompError('model')
          break
      }
    }
    try {
      await sendTurn(id, text, handle, abort.current.signal)
    } catch (e) {
      if (e instanceof ApiError && e.detail === 'session_exhausted') setCompError('exhausted')
      else if (e instanceof ApiError && e.detail === 'busy_or_closed') {
        setDraft(text)
        setCompError('model')
      } else if (!(e instanceof DOMException && e.name === 'AbortError')) setCompError('model')
    } finally {
      setToolLine('')
      setStreaming(false)
      setStreamText('')
      setBusy(false)
    }
    const done = finished as TurnEvent['data'] | null
    if (done && 'turn' in done) {
      const reply = streamed.trim()
      const crisisReply = isCrisisReply(reply)
      if (reply)
        setThread((t) => t.concat({ role: 'companion', text: reply, crisis: crisisReply }))
      setCrisis(crisisReply)
      setTurn(done.turn)
      setTurnsLeft(done.turns_left)
      if (done.awaiting_confirmation && proposed !== null) {
        readCredits()
        // The brief itself lives on the session; one read fills the card.
        try {
          const t = await getSession(id)
          if (t.pending)
            setProposal({ ...t.pending, summary: summarise(t.pending.brief) })
          else setProposal({ brief: '', duration_minutes: proposed, summary: '' })
        } catch {
          setProposal({ brief: '', duration_minutes: proposed, summary: '' })
        }
      } else {
        setProposal(null)
      }
      if (done.turns_left <= 0) setCompError('exhausted')
    }
  }

  const send = useCallback(async () => {
    const text = draft.trim()
    if (!text || busy) return
    if (turnsLeft <= 0) {
      setCompError('exhausted')
      return
    }
    const id = await ensureSession()
    if (!id) return
    lastSent.current = text
    setThread((t) => t.concat({ role: 'user', text }))
    setDraft('')
    setProposal(null)
    setBriefOpen(false)
    setChanging(false)
    setCrisis(false)
    setCompError(null)
    await respond(id, text)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reads current draft/busy only
  }, [draft, busy, turnsLeft, sessionId])

  const sendAgain = useCallback(async () => {
    const text = lastSent.current
    if (!text || busy) return
    const id = await ensureSession()
    if (!id) return
    setCompError(null)
    await respond(id, text)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busy, sessionId])

  /** Confirm the proposal: the one tap in the feature that spends a credit. */
  const start = useCallback(async () => {
    if (!sessionId || !proposal || starting) return
    setStarting(true)
    setCompError(null)
    // As on Home's Begin: the music starts inside the tap (the only place a
    // mobile browser allows) and plays through the wait; a refused start
    // silences it again. It lives here, not on a button, so every path that
    // starts -- the card, the error card's retry -- behaves the same.
    void mixer.startAmbient(bgmUrl(DEFAULT_BGM_TRACK))
    try {
      const { job_id } = await confirmSession(sessionId)
      remember(null)
      onStarted(job_id, proposal.duration_minutes)
    } catch (e) {
      mixer.stopAmbient()
      if (e instanceof ApiError && e.detail === 'no_credit') setCompError('nocredit')
      else if (e instanceof ApiError && e.detail === 'nothing_to_confirm') setProposal(null)
      else setCompError('start')
    } finally {
      setStarting(false)
    }
  }, [sessionId, proposal, starting, onStarted])

  const startOver = useCallback(() => {
    abort.current?.abort()
    if (sessionId) abandonSession(sessionId).catch(() => undefined)
    remember(null)
    setThread([])
    setDraft('')
    setToolLine('')
    setStreaming(false)
    setStreamText('')
    setProposal(null)
    setBriefOpen(false)
    setChanging(false)
    setTurn(0)
    setTurnsLeft(MAX_TURNS)
    setCompError(null)
    setCrisis(false)
    setStarting(false)
    setBusy(false)
  }, [sessionId])

  const state: CompanionState = {
    gate,
    thread,
    draft,
    toolLine,
    streaming,
    streamText,
    proposal,
    briefOpen,
    changing,
    turn,
    turnsLeft,
    compError,
    starting,
    crisis,
    credits,
    insightsCount,
    busy,
  }
  return {
    state,
    setDraft,
    send,
    sendAgain,
    start,
    startOver,
    beginChange: () => setChanging(true),
    openBrief: () => setBriefOpen(true),
    closeBrief: () => setBriefOpen(false),
    dismissError: () => setCompError(null),
  }
}
