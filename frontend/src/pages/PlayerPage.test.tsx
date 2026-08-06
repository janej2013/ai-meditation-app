/**
 * Reaching the player without the handoff from GeneratingPage.
 *
 * The signed narration URL lives in router state and expires after fifteen
 * minutes, so a reload or an idle tab arrives with nothing to play. The job id
 * is in the path either way, and the recording has already been paid for --
 * these assert it is recoverable rather than lost.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, getJob: vi.fn() }
})

const loadNarration = vi.fn()
const loadBgm = vi.fn()

vi.mock('../audio/mixer', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../audio/mixer')>()
  return {
    ...actual,
    DualTrackMixer: class {
      onEnded: (() => void) | null = null
      loadNarration = loadNarration
      loadBgm = loadBgm
      duration = () => 600
      elapsed = () => 0
      isPlaying = () => false
      dispose = vi.fn()
    },
  }
})

import { NotSignedInError, getJob } from '../api/client'
import PlayerPage from './PlayerPage'

const JOB_ID = 'job-abc'
const FRESH_URL = 'https://d111.cloudfront.net/jobs/job-abc/narration.mp3?Signature=new'

function renderPlayer(state?: { audioUrl?: string }) {
  return render(
    <MemoryRouter initialEntries={[{ pathname: `/player/${JOB_ID}`, state: state ?? null }]}>
      <Routes>
        <Route path="/player/:jobId" element={<PlayerPage />} />
        <Route path="/" element={<div>HOME SCREEN</div>} />
        <Route path="/signup" element={<div>SIGNUP SCREEN</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

function job(audioUrl: string | null) {
  return { job_id: JOB_ID, status: 'DONE' as const, audio_url: audioUrl }
}

beforeEach(() => {
  // restoreAllMocks does not clear call history on these module-factory
  // vi.fn()s, and one case asserts getJob was *not* called -- without this it
  // sees the previous cases' calls.
  vi.clearAllMocks()
  loadNarration.mockResolvedValue(undefined)
  loadBgm.mockResolvedValue(undefined)
  vi.mocked(getJob).mockResolvedValue(job(FRESH_URL))
})

afterEach(() => vi.restoreAllMocks())

describe('PlayerPage recovery', () => {
  it('signs a fresh URL when arriving without router state', async () => {
    renderPlayer()

    await waitFor(() => expect(loadNarration).toHaveBeenCalledWith(FRESH_URL))
    expect(getJob).toHaveBeenCalledWith(JOB_ID)
  })

  it('re-signs when the handed-over URL has expired', async () => {
    loadNarration.mockRejectedValueOnce(new Error('audio fetch failed: 403'))

    renderPlayer({ audioUrl: 'https://d111.cloudfront.net/jobs/job-abc/narration.mp3?expired' })

    await waitFor(() => expect(loadNarration).toHaveBeenLastCalledWith(FRESH_URL))
  })

  it('does not call the API when the handoff still works', async () => {
    const handoff = 'https://d111.cloudfront.net/jobs/job-abc/narration.mp3?Signature=live'

    renderPlayer({ audioUrl: handoff })

    await waitFor(() => expect(loadNarration).toHaveBeenCalledWith(handoff))
    expect(getJob).not.toHaveBeenCalled()
  })

  it('sends an expired session to sign in, not to the failure screen', async () => {
    vi.mocked(getJob).mockRejectedValue(new NotSignedInError())

    renderPlayer()

    expect(await screen.findByText('SIGNUP SCREEN')).toBeInTheDocument()
  })

  it('reports a job that really has no audio', async () => {
    vi.mocked(getJob).mockResolvedValue(job(null))

    renderPlayer()

    expect(await screen.findByText(/could not be loaded/i)).toBeInTheDocument()
  })
})
