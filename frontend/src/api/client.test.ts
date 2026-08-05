/**
 * The API client: header discipline, error mapping, and the poll loop.
 * The ID-token rule is the one that breaks silently in production, so it is
 * asserted directly here.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../auth/cognito', () => ({
  getIdToken: vi.fn(),
}))

import { getIdToken } from '../auth/cognito'
import { ApiError, NotSignedInError, getAccount, pollJob, startGeneration } from './client'

const mockToken = vi.mocked(getIdToken)

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

beforeEach(() => {
  vi.stubEnv('VITE_API_URL', 'https://api.example.com')
  mockToken.mockResolvedValue('id-token-123')
  vi.stubGlobal('fetch', vi.fn())
})

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('request plumbing', () => {
  it('sends the ID token as a bearer header', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockResolvedValue(jsonResponse(200, { available: 1, frozen: 0, plan: 'free' }))

    await getAccount()

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('https://api.example.com/account')
    expect((init?.headers as Record<string, string>).Authorization).toBe('Bearer id-token-123')
  })

  it('throws NotSignedInError before any network call when there is no session', async () => {
    mockToken.mockResolvedValue(null)

    await expect(getAccount()).rejects.toBeInstanceOf(NotSignedInError)
    expect(fetch).not.toHaveBeenCalled()
  })

  it('maps a non-2xx to ApiError carrying status and detail', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(402, { detail: 'No generations remaining.' }),
    )

    const error = await getAccount().catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(402)
    expect((error as ApiError).detail).toBe('No generations remaining.')
  })

  it('sends the generate body in the backend contract shape', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockResolvedValue(jsonResponse(202, { job_id: 'j1', status: 'PENDING' }))

    await startGeneration('anxious', 10)

    const [, init] = fetchMock.mock.calls[0]
    expect(JSON.parse(init?.body as string)).toEqual({
      mood: 'anxious',
      duration_minutes: 10,
    })
  })
})

describe('pollJob', () => {
  it('polls until the job is DONE and reports updates', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse(200, { job_id: 'j1', status: 'GENERATING', audio_url: null }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, { job_id: 'j1', status: 'DONE', audio_url: 'https://signed' }),
      )

    const seen: string[] = []
    const job = await pollJob('j1', {
      onUpdate: (j) => seen.push(j.status),
      initialIntervalMs: 1,
    })

    expect(job.status).toBe('DONE')
    expect(job.audio_url).toBe('https://signed')
    expect(seen).toEqual(['GENERATING', 'DONE'])
  })

  it('stops on FAILED without treating it as an error', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(200, { job_id: 'j1', status: 'FAILED', audio_url: null }),
    )

    const job = await pollJob('j1', { initialIntervalMs: 1 })

    expect(job.status).toBe('FAILED')
  })

  it('aborts cleanly through the signal', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(200, { job_id: 'j1', status: 'GENERATING', audio_url: null }),
    )
    const controller = new AbortController()
    const pending = pollJob('j1', { signal: controller.signal, initialIntervalMs: 5000 })

    controller.abort()

    await expect(pending).rejects.toHaveProperty('name', 'AbortError')
  })
})
