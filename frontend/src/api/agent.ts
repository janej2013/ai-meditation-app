/**
 * The companion's client. Same origin as the PWA: CloudFront routes
 * `/agent/*` to the agent Lambda, so there is no CORS and the base path is
 * empty in production. Locally, Vite proxies `/agent` to the runner.
 *
 * Two things differ from `client.ts`:
 *
 * - the ID token travels in `X-Id-Token`, not `Authorization`: CloudFront's
 *   origin access control overwrites `Authorization` with its own signature on
 *   the way to the Function URL, so a bearer token sent there never arrives;
 * - every POST and DELETE states its body's SHA-256 in `x-amz-content-sha256`,
 *   which that signature requires.
 *
 * Streaming replies are read with `fetch` (see companion/sse.ts).
 */
import { getIdToken } from '../auth/cognito'
import { readSse, type SseEvent } from '../companion/sse'
import { sha256Hex } from '../companion/sha256'
import { ApiError, NotSignedInError } from './client'

/**
 * Which engine answers: the two run as separate functions behind
 * `/agent/*` (native, the default) and `/agent-lg/*` (LangGraph). The path
 * is the only thing the client changes; the contract is the same.
 */
export type Engine = 'native' | 'langgraph'

export function agentBase(engine: Engine = 'native'): string {
  const host: string = import.meta.env.VITE_AGENT_BASE ?? ''
  return `${host}${engine === 'langgraph' ? '/agent-lg' : '/agent'}`
}

export interface SessionCreated {
  session_id: string
  turn: number
  engine: string
  model_id: string
  insights_count: number
}

export interface PendingProposal {
  brief: string
  duration_minutes: number
}

export interface TranscriptTurn {
  turn: number
  user_text: string
  assistant_text: string
  tools: string[]
  created_at: string | null
}

export interface Transcript {
  session_id: string
  status: 'ACTIVE' | 'FINALIZED' | 'ABANDONED' | 'FAILED'
  turn: number
  job_id: string | null
  pending: PendingProposal | null
  turns: TranscriptTurn[]
}

export interface Insight {
  text: string
  created_at: string
}

export interface Memory {
  insights: Insight[]
  sessions_this_month: number
  sessions_per_month: number
}

/** The runner's turn events, as the hook consumes them. */
export type TurnEvent =
  | { event: 'delta'; data: { text: string } }
  | { event: 'tool'; data: { name: string } }
  | { event: 'proposal'; data: { duration_minutes: number } }
  | {
      event: 'done'
      data: { turn: number; job_id: string | null; awaiting_confirmation: boolean; turns_left: number }
    }
  | { event: 'error'; data: { code: string; retryable: boolean } }

async function headers(extra?: Record<string, string>): Promise<Headers> {
  const token = await getIdToken()
  if (!token) throw new NotSignedInError()
  return new Headers({ 'X-Id-Token': token, ...extra })
}

async function request<T>(
  method: 'GET' | 'POST' | 'DELETE',
  path: string,
  body?: unknown,
): Promise<T> {
  const text = body === undefined ? '' : JSON.stringify(body)
  const h = await headers()
  if (method !== 'GET') {
    h.set('x-amz-content-sha256', await sha256Hex(text))
    if (text) h.set('Content-Type', 'application/json')
  }
  const response = await fetch(path, {
    method,
    headers: h,
    body: text || undefined,
  })
  if (!response.ok) throw new ApiError(response.status, await detailOf(response))
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

async function detailOf(response: Response): Promise<string> {
  try {
    return ((await response.json()) as { detail?: string }).detail ?? response.statusText
  } catch {
    return response.statusText
  }
}

export function createSession(engine: Engine = 'native'): Promise<SessionCreated> {
  return request<SessionCreated>('POST', `${agentBase(engine)}/sessions`)
}

export function getSession(sessionId: string, engine: Engine = 'native'): Promise<Transcript> {
  return request<Transcript>(
    'GET',
    `${agentBase(engine)}/sessions/${encodeURIComponent(sessionId)}`,
  )
}

export function abandonSession(sessionId: string, engine: Engine = 'native'): Promise<void> {
  return request<void>(
    'POST',
    `${agentBase(engine)}/sessions/${encodeURIComponent(sessionId)}/abandon`,
  )
}

export function confirmSession(
  sessionId: string,
  engine: Engine = 'native',
): Promise<{ job_id: string }> {
  return request<{ job_id: string }>(
    'POST',
    `${agentBase(engine)}/sessions/${encodeURIComponent(sessionId)}/confirm`,
  )
}

// Memory is one item on the table, whoever wrote it: always the default path.
export function getMemory(): Promise<Memory> {
  return request<Memory>('GET', `${agentBase()}/memory`)
}

export function clearMemory(): Promise<void> {
  return request<void>('DELETE', `${agentBase()}/memory`)
}

/**
 * One turn, streamed. Resolves when the stream ends; every event goes to
 * `onEvent` in order. A non-2xx status (409 busy_or_closed, 409
 * session_exhausted, 404, 422) is thrown before any event.
 */
export async function sendTurn(
  sessionId: string,
  text: string,
  onEvent: (event: TurnEvent) => void,
  signal?: AbortSignal,
  engine: Engine = 'native',
): Promise<void> {
  const body = JSON.stringify({ text })
  const h = await headers({
    'Content-Type': 'application/json',
    Accept: 'text/event-stream',
  })
  h.set('x-amz-content-sha256', await sha256Hex(body))
  const response = await fetch(`${agentBase(engine)}/sessions/${encodeURIComponent(sessionId)}/turns`, {
    method: 'POST',
    headers: h,
    body,
    signal,
  })
  if (!response.ok) throw new ApiError(response.status, await detailOf(response))
  if (!response.body) throw new ApiError(502, 'No stream in the response')
  for await (const event of readSse(response.body, signal)) {
    onEvent(event as SseEvent as TurnEvent)
  }
}
