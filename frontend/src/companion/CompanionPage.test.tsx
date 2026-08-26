/**
 * The ten artboards as states of the page, driven by mocked runner calls.
 * `agent.ts` is mocked at the module boundary: the SSE parsing has its own
 * tests, and here each turn is a scripted list of events.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/agent', () => ({
  createSession: vi.fn(),
  sendTurn: vi.fn(),
  confirmSession: vi.fn(),
  getSession: vi.fn(),
  abandonSession: vi.fn(),
  getMemory: vi.fn(),
}))
vi.mock('../audio/mixer', () => ({
  mixer: { startAmbient: vi.fn(), stopAmbient: vi.fn() },
  bgmUrl: () => 'https://audio.test/bgm.mp3',
  DEFAULT_BGM_TRACK: { path: 'assets/bgm/default_bgm.mp3' },
}))
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, getAccount: vi.fn() }
})

import {
  abandonSession,
  confirmSession,
  createSession,
  getMemory,
  getSession,
  sendTurn,
  type TurnEvent,
} from '../api/agent'
import { ApiError, getAccount } from '../api/client'
import { mixer } from '../audio/mixer'
import CompanionPage from './CompanionPage'
import { MAX_TURNS } from './useCompanion'

const CRISIS =
  "It sounds like you might be going through something really hard right now, and I'm glad you said it. I'm a meditation companion, not a crisis service, so please reach out to people who can help right now: Lifeline on 13 11 14 (24 hours), Beyond Blue on 1300 22 4636, or 000 if you or someone else is in immediate danger."

/** The waiting screen's stand-in, showing the duration it was handed. */
function GeneratingProbe() {
  const { state } = useLocation() as { state: { duration?: number } | null }
  return <div>GENERATING SCREEN {state?.duration ?? 'no duration'}</div>
}

function renderPage(path = '/companion') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/companion" element={<CompanionPage />} />
        <Route path="/generating/:jobId" element={<GeneratingProbe />} />
        <Route path="/plans" element={<div>PLANS SCREEN</div>} />
        <Route path="/" element={<div>HOME SCREEN</div>} />
        <Route path="/signup" element={<div>SIGNUP SCREEN</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

/** Script the next turn: the events the runner would stream, in order. */
function scriptTurn(events: TurnEvent[], opts: { reject?: Error } = {}) {
  vi.mocked(sendTurn).mockImplementationOnce(async (_id, _text, onEvent) => {
    if (opts.reject) throw opts.reject
    for (const e of events) onEvent(e)
  })
}

const done = (turn: number, awaiting = false): TurnEvent => ({
  event: 'done',
  data: { turn, job_id: null, awaiting_confirmation: awaiting, turns_left: MAX_TURNS - turn },
})
const delta = (text: string): TurnEvent => ({ event: 'delta', data: { text } })
const tool = (name: string): TurnEvent => ({ event: 'tool', data: { name } })

async function say(text: string) {
  fireEvent.change(screen.getByLabelText('Message'), { target: { value: text } })
  await act(async () => {
    fireEvent.click(screen.getByLabelText('Send'))
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  vi.mocked(getAccount).mockResolvedValue({ available: 95, frozen: 0, plan: 'pro' })
  vi.mocked(createSession).mockResolvedValue({
    session_id: 's1',
    turn: 0,
    engine: 'native',
    model_id: 'm',
    insights_count: 0,
  })
  vi.mocked(getSession).mockResolvedValue({
    session_id: 's1',
    status: 'ACTIVE',
    turn: 1,
    job_id: null,
    pending: null,
    turns: [],
  })
  vi.mocked(abandonSession).mockResolvedValue(undefined)
  vi.mocked(getMemory).mockResolvedValue({ insights: [], sessions_this_month: 0, sessions_per_month: 30 })
})
afterEach(() => vi.restoreAllMocks())

describe('CompanionPage', () => {
  it('[empty] opens with the invitation and no session yet', async () => {
    renderPage()

    expect(screen.getByText(/Tell me how tonight feels/)).toBeInTheDocument()
    expect(screen.queryByText(/I remember a few things/)).not.toBeInTheDocument()
    expect(createSession).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Send')).toBeDisabled()
    // The shell's pill already reads the account on a reload; the page does
    // not add a second read until there is a card to put the number on.
    expect(getAccount).not.toHaveBeenCalled()
  })

  it('[empty] mentions memory when it remembers something', async () => {
    vi.mocked(getMemory).mockResolvedValue({
      insights: [{ text: 'Prefers slow narration', created_at: '2026-08-26T09:00:00+00:00' }],
      sessions_this_month: 2,
      sessions_per_month: 30,
    })
    renderPage()

    expect(await screen.findByText(/I remember a few things you told me/)).toBeInTheDocument()
    expect(createSession).not.toHaveBeenCalled()
  })

  it('[empty] stays quiet when the memory read is refused', async () => {
    vi.mocked(getMemory).mockRejectedValue(new ApiError(403, 'plan_required'))
    renderPage()

    expect(await screen.findByText(/Tell me how tonight feels/)).toBeInTheDocument()
    await waitFor(() => expect(getMemory).toHaveBeenCalled())
    expect(screen.queryByText(/I remember a few things/)).not.toBeInTheDocument()
    expect(screen.queryByText('Companion is part of Pro')).not.toBeInTheDocument()
  })

  it('[streaming] shows the tool line, then the reply, then commits it', async () => {
    let release!: () => void
    const gate = new Promise<void>((r) => (release = r))
    vi.mocked(sendTurn).mockImplementationOnce(async (_id, _text, onEvent) => {
      onEvent(tool('get_session_history'))
      await gate
      onEvent(delta('That gap '))
      onEvent(delta('between tired and asleep.'))
      onEvent(done(1))
    })
    renderPage()

    await say('tired but wired')

    expect(screen.getByText('tired but wired')).toBeInTheDocument()
    expect(await screen.findByText('Looking back at your earlier sessions…')).toBeInTheDocument()
    await act(async () => release())
    expect(await screen.findByText('That gap between tired and asleep.')).toBeInTheDocument()
    expect(screen.queryByText('Looking back at your earlier sessions…')).not.toBeInTheDocument()
    expect(sendTurn).toHaveBeenCalledWith(
      's1',
      'tired but wired',
      expect.any(Function),
      expect.anything(),
      'native',
    )
  })

  it('[proposal] fills the card from the pending brief and starts on confirm', async () => {
    scriptTurn([
      tool('finalize_meditation_brief'),
      { event: 'proposal', data: { duration_minutes: 10 } },
      delta('Start it whenever you like.'),
      done(1, true),
    ])
    vi.mocked(getSession).mockResolvedValue({
      session_id: 's1',
      status: 'ACTIVE',
      turn: 1,
      job_id: null,
      pending: { brief: 'Slow narration over a shoreline at dusk, long pauses. '.repeat(3), duration_minutes: 10 },
      turns: [],
    })
    vi.mocked(confirmSession).mockResolvedValue({ job_id: 'job-9' })
    renderPage()

    await say('go ahead')

    expect(await screen.findByText('10 min')).toBeInTheDocument()
    expect(screen.getByText('Uses 1 credit · 95 left')).toBeInTheDocument()
    expect(screen.getByText(/Slow narration over a shoreline/)).toBeInTheDocument()
    expect(screen.getByLabelText('Message')).toHaveAttribute('placeholder', 'or tell me what to change…')

    fireEvent.click(screen.getByText('Read the brief'))
    expect(screen.getByText('Hide the brief')).toBeInTheDocument()

    await act(async () => {
      fireEvent.click(screen.getByText('Start the meditation'))
    })

    expect(confirmSession).toHaveBeenCalledWith('s1', 'native')
    expect(await screen.findByText('GENERATING SCREEN 10')).toBeInTheDocument()
    expect(mixer.startAmbient).toHaveBeenCalledWith('https://audio.test/bgm.mp3')
    expect(mixer.stopAmbient).not.toHaveBeenCalled()
    expect(sessionStorage.getItem('drift:companion-session')).toBeNull()
  })

  it('[proposal] Change something keeps the card; a new message withdraws it', async () => {
    scriptTurn([{ event: 'proposal', data: { duration_minutes: 5 } }, delta('Ready.'), done(1, true)])
    vi.mocked(getSession).mockResolvedValue({
      session_id: 's1',
      status: 'ACTIVE',
      turn: 1,
      job_id: null,
      pending: { brief: 'A short brief.', duration_minutes: 5 },
      turns: [],
    })
    renderPage()
    await say('go')
    expect(await screen.findByText('5 min')).toBeInTheDocument()

    fireEvent.click(screen.getByText('Change something'))
    expect(screen.getByText('5 min')).toBeInTheDocument()
    expect(screen.getByText('Start the meditation')).toBeInTheDocument()
    expect(screen.getByText(/Tell me what you'd like different/)).toBeInTheDocument()
    expect(screen.queryByText('Change something')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Message')).toHaveFocus()

    scriptTurn([delta('What would you change?'), done(2, false)])
    await say('something different')

    await screen.findByText('What would you change?')
    expect(screen.queryByText('5 min')).not.toBeInTheDocument()
    expect(screen.queryByText(/Tell me what you'd like different/)).not.toBeInTheDocument()
  })

  it('[errors] model_unavailable offers Send again, which resends the same text', async () => {
    scriptTurn([{ event: 'error', data: { code: 'model_unavailable', retryable: true } }])
    renderPage()

    await say('hello there')

    expect(await screen.findByText(/Something went quiet on my side/)).toBeInTheDocument()
    scriptTurn([delta('Back now.'), done(1)])
    await act(async () => {
      fireEvent.click(screen.getByText('Send again'))
    })
    expect(await screen.findByText('Back now.')).toBeInTheDocument()
    expect(vi.mocked(sendTurn).mock.calls[1][1]).toBe('hello there')
  })

  it('[errors] an exhausted session offers Start over, which abandons it', async () => {
    scriptTurn([], { reject: new ApiError(409, 'session_exhausted') })
    renderPage()

    await say('more')

    expect(await screen.findByText(/We've talked for a while/)).toBeInTheDocument()
    await act(async () => {
      fireEvent.click(screen.getByText('Start over'))
    })
    expect(abandonSession).toHaveBeenCalledWith('s1', 'native')
    expect(screen.getByText(/Tell me how tonight feels/)).toBeInTheDocument()
  })

  it('[errors] turns_left reaching zero also reads as exhausted', async () => {
    scriptTurn([delta('Last one.'), { event: 'done', data: { turn: 12, job_id: null, awaiting_confirmation: false, turns_left: 0 } }])
    renderPage()

    await say('final')

    expect(await screen.findByText(/We've talked for a while/)).toBeInTheDocument()
  })

  it('[errors] no credit on confirm keeps the card and links to Plans', async () => {
    scriptTurn([{ event: 'proposal', data: { duration_minutes: 5 } }, delta('Ready.'), done(1, true)])
    vi.mocked(getSession).mockResolvedValue({
      session_id: 's1',
      status: 'ACTIVE',
      turn: 1,
      job_id: null,
      pending: { brief: 'A short brief.', duration_minutes: 5 },
      turns: [],
    })
    vi.mocked(confirmSession).mockRejectedValue(new ApiError(402, 'no_credit'))
    renderPage()
    await say('go')
    await screen.findByText('5 min')

    await act(async () => {
      fireEvent.click(screen.getByText('Start the meditation'))
    })

    expect(await screen.findByText('No generations left')).toBeInTheDocument()
    expect(screen.getByText('5 min')).toBeInTheDocument()
    // The music began inside the tap; a refused start silences it again.
    expect(mixer.stopAmbient).toHaveBeenCalled()
    fireEvent.click(screen.getByText('Add credits'))
    expect(await screen.findByText('PLANS SCREEN')).toBeInTheDocument()
  })

  it('[errors] Try again after a failed start plays the music like the first tap', async () => {
    scriptTurn([{ event: 'proposal', data: { duration_minutes: 5 } }, delta('Ready.'), done(1, true)])
    vi.mocked(getSession).mockResolvedValue({
      session_id: 's1',
      status: 'ACTIVE',
      turn: 1,
      job_id: null,
      pending: { brief: 'A short brief.', duration_minutes: 5 },
      turns: [],
    })
    vi.mocked(confirmSession).mockRejectedValueOnce(new ApiError(503, 'start_failed'))
    vi.mocked(confirmSession).mockResolvedValueOnce({ job_id: 'job-9' })
    renderPage()
    await say('go')
    await screen.findByText('5 min')

    await act(async () => {
      fireEvent.click(screen.getByText('Start the meditation'))
    })
    expect(await screen.findByText(/Couldn't start it just now/)).toBeInTheDocument()
    expect(mixer.stopAmbient).toHaveBeenCalledTimes(1)

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    })

    expect(await screen.findByText(/GENERATING SCREEN/)).toBeInTheDocument()
    expect(mixer.startAmbient).toHaveBeenCalledTimes(2)
    expect(mixer.stopAmbient).toHaveBeenCalledTimes(1)
  })

  it('[plan_required] a free account sees the Pro screen', async () => {
    vi.mocked(createSession).mockRejectedValue(new ApiError(403, 'plan_required'))
    renderPage()

    await say('hello')

    expect(await screen.findByText('Companion is part of Pro')).toBeInTheDocument()
    fireEvent.click(screen.getByText('See Pro'))
    expect(await screen.findByText('PLANS SCREEN')).toBeInTheDocument()
  })

  it('[crisis reply] is set apart, with tappable numbers, and no proposal card', async () => {
    scriptTurn([delta(CRISIS), done(1)])
    renderPage()

    await say("honestly I don't know how much longer I can keep doing this")

    const lifeline = await screen.findByRole('link', { name: '13 11 14' })
    expect(lifeline).toHaveAttribute('href', 'tel:131114')
    expect(screen.getByRole('link', { name: '000' })).toHaveAttribute('href', 'tel:000')
    expect(lifeline.closest('.crisis-block')).not.toBeNull()
    expect(screen.queryByText('Start the meditation')).not.toBeInTheDocument()
  })

  it('[streaming] shows the replies-left counter near the end', async () => {
    scriptTurn([delta('ok'), { event: 'done', data: { turn: 9, job_id: null, awaiting_confirmation: false, turns_left: 3 } }])
    renderPage()

    await say('hello')

    expect(await screen.findByText('3 replies left')).toBeInTheDocument()
  })

  it('resumes a stored session on reload', async () => {
    sessionStorage.setItem('drift:companion-session', 's1')
    vi.mocked(getSession).mockResolvedValue({
      session_id: 's1',
      status: 'ACTIVE',
      turn: 2,
      job_id: null,
      pending: { brief: 'Kept brief.', duration_minutes: 8 },
      turns: [
        { turn: 0, user_text: 'earlier', assistant_text: 'I remember.', tools: [], created_at: null },
        { turn: 1, user_text: 'go', assistant_text: 'Ready when you are.', tools: [], created_at: null },
      ],
    })
    renderPage()

    expect(await screen.findByText('I remember.')).toBeInTheDocument()
    expect(screen.getByText('earlier')).toBeInTheDocument()
    expect(screen.getByText('8 min')).toBeInTheDocument()
    expect(createSession).not.toHaveBeenCalled()
  })

  it('[engine] ?engine=langgraph opens the session on the other path', async () => {
    scriptTurn([delta('From the graph.'), done(1)])
    renderPage('/companion?engine=langgraph')

    expect(document.querySelector('.companion')?.getAttribute('data-engine')).toBe('langgraph')
    await say('hello')

    expect(createSession).toHaveBeenCalledWith('langgraph')
    expect(await screen.findByText('From the graph.')).toBeInTheDocument()
    expect(sendTurn).toHaveBeenCalledWith('s1', 'hello', expect.any(Function), expect.anything(), 'langgraph')
    expect(JSON.parse(sessionStorage.getItem('drift:companion-session') ?? '{}')).toEqual({
      id: 's1',
      engine: 'langgraph',
    })
  })

  it('[engine] a reload resumes on the engine the session was opened on', async () => {
    sessionStorage.setItem('drift:companion-session', JSON.stringify({ id: 's1', engine: 'langgraph' }))
    renderPage('/companion?engine=langgraph')

    await waitFor(() => expect(getSession).toHaveBeenCalledWith('s1', 'langgraph'))
    expect(createSession).not.toHaveBeenCalled()
  })

  it('[engine] a stored session on the other engine is let go, not resumed', async () => {
    sessionStorage.setItem('drift:companion-session', JSON.stringify({ id: 'old', engine: 'langgraph' }))
    scriptTurn([delta('Fresh start.'), done(1)])
    renderPage('/companion')

    expect(abandonSession).toHaveBeenCalledWith('old', 'langgraph')
    expect(getSession).not.toHaveBeenCalled()
    expect(sessionStorage.getItem('drift:companion-session')).toBeNull()
    await say('hello')
    expect(createSession).toHaveBeenCalledWith('native')
  })

  it('[engine] the pre-L2 storage format still resumes, as native', async () => {
    sessionStorage.setItem('drift:companion-session', 's1')
    renderPage()

    await waitFor(() => expect(getSession).toHaveBeenCalledWith('s1', 'native'))
  })

  it('a closed stored session is forgotten', async () => {
    sessionStorage.setItem('drift:companion-session', 'old')
    vi.mocked(getSession).mockResolvedValue({
      session_id: 'old',
      status: 'FINALIZED',
      turn: 3,
      job_id: 'job-1',
      pending: null,
      turns: [],
    })
    renderPage()

    await waitFor(() => expect(sessionStorage.getItem('drift:companion-session')).toBeNull())
    expect(screen.getByText(/Tell me how tonight feels/)).toBeInTheDocument()
  })
})
