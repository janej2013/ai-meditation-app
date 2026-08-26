/**
 * The agent client's request shapes: the ID token in X-Id-Token (never
 * Authorization -- CloudFront's OAC overwrites that), the payload hash on
 * every POST and DELETE, and the SSE events surfaced in order.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../auth/cognito', () => ({ getIdToken: vi.fn() }))

import { getIdToken } from '../auth/cognito'
import { EMPTY_BODY_SHA256 } from '../companion/sha256'
import {
  agentBase,
  clearMemory,
  confirmSession,
  createSession,
  getMemory,
  getSession,
  sendTurn,
  type TurnEvent,
} from './agent'
import { ApiError } from './client'

const fetchMock = vi.fn()

function jsonResponse(status: number, body: unknown) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function sseResponse(text: string) {
  return new Response(new TextEncoder().encode(text), {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  })
}

function lastCall() {
  const [url, init] = fetchMock.mock.calls.at(-1) as [string, RequestInit]
  return { url, init, headers: init.headers as Headers }
}

beforeEach(() => {
  vi.mocked(getIdToken).mockResolvedValue('id.token.here')
  vi.stubGlobal('fetch', fetchMock)
})
afterEach(() => {
  vi.unstubAllGlobals()
  fetchMock.mockReset()
})

describe('agent client', () => {
  it('creates a session with the token header and the empty-body hash', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(201, {
        session_id: 's1',
        turn: 0,
        engine: 'native',
        model_id: 'm',
        insights_count: 0,
      }),
    )

    const created = await createSession()

    expect(created.session_id).toBe('s1')
    const { url, init, headers } = lastCall()
    expect(url).toBe('/agent/sessions')
    expect(init.method).toBe('POST')
    expect(headers.get('X-Id-Token')).toBe('id.token.here')
    expect(headers.get('Authorization')).toBeNull()
    expect(headers.get('x-amz-content-sha256')).toBe(EMPTY_BODY_SHA256)
  })

  it('the LangGraph engine is the other path prefix, nothing else', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(201, {
        session_id: 's2',
        turn: 0,
        engine: 'langgraph',
        model_id: 'm',
        insights_count: 0,
      }),
    )

    await createSession('langgraph')

    const { url, headers } = lastCall()
    expect(url).toBe('/agent-lg/sessions')
    expect(headers.get('X-Id-Token')).toBe('id.token.here')
    expect(agentBase()).toBe('/agent')
    expect(agentBase('langgraph')).toBe('/agent-lg')
  })

  it('memory is the same item for both engines: always the default path', async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }))

    await clearMemory()

    expect(lastCall().url).toBe('/agent/memory')
  })

  it('GET carries the token and no hash', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, { insights: [], sessions_this_month: 1, sessions_per_month: 30 }),
    )

    await getMemory()

    const { init, headers } = lastCall()
    expect(init.method).toBe('GET')
    expect(headers.get('X-Id-Token')).toBe('id.token.here')
    expect(headers.get('x-amz-content-sha256')).toBeNull()
  })

  it('DELETE carries the empty-body hash', async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }))

    await clearMemory()

    const { url, init, headers } = lastCall()
    expect(url).toBe('/agent/memory')
    expect(init.method).toBe('DELETE')
    expect(headers.get('x-amz-content-sha256')).toBe(EMPTY_BODY_SHA256)
  })

  it('surfaces the backend detail code on errors', async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, { detail: 'nothing_to_confirm' }))

    await expect(confirmSession('s1')).rejects.toMatchObject(
      new ApiError(409, 'nothing_to_confirm'),
    )
  })

  it('reads the transcript', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, {
        session_id: 's1',
        status: 'ACTIVE',
        turn: 1,
        job_id: null,
        pending: { brief: 'b', duration_minutes: 5 },
        turns: [],
      }),
    )

    expect((await getSession('s1')).pending).toEqual({ brief: 'b', duration_minutes: 5 })
    expect(lastCall().url).toBe('/agent/sessions/s1')
  })

  it('streams a turn: hashed JSON body, events in order', async () => {
    fetchMock.mockResolvedValue(
      sseResponse(
        'event: tool\ndata: {"name":"save_user_insight"}\n\n: ping\n\nevent: delta\ndata: {"text":"Noted"}\n\n' +
          'event: proposal\ndata: {"duration_minutes":10}\n\nevent: done\ndata: {"turn":1,"job_id":null,"awaiting_confirmation":true,"turns_left":11}\n\n',
      ),
    )
    const events: TurnEvent[] = []

    await sendTurn('s1', 'slow please', (e) => events.push(e))

    const { url, init, headers } = lastCall()
    expect(url).toBe('/agent/sessions/s1/turns')
    expect(init.body).toBe(JSON.stringify({ text: 'slow please' }))
    expect(headers.get('Content-Type')).toBe('application/json')
    expect(headers.get('x-amz-content-sha256')).toMatch(/^[0-9a-f]{64}$/)
    expect(headers.get('x-amz-content-sha256')).not.toBe(EMPTY_BODY_SHA256)
    expect(events.map((e) => e.event)).toEqual(['tool', 'delta', 'proposal', 'done'])
  })

  it('throws the status before any event when the turn is refused', async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, { detail: 'busy_or_closed' }))
    const events: TurnEvent[] = []

    await expect(sendTurn('s1', 'hi', (e) => events.push(e))).rejects.toMatchObject(
      new ApiError(409, 'busy_or_closed'),
    )
    expect(events).toEqual([])
  })
})
