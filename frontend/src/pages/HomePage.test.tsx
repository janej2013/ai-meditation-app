/**
 * The home flow's decision tree: the sentence opens the panel, chips (or own
 * words) gate Begin drifting, the destination folds into the mood string, and
 * the API's 402/429 behaviours route the way the UI promises.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SceneProvider } from '../scene/SceneContext'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    getAccount: vi.fn(),
    startGeneration: vi.fn(),
  }
})
vi.mock('../auth/cognito', () => ({
  isSignedIn: vi.fn(),
}))

import { ApiError, getAccount, startGeneration } from '../api/client'
import { isSignedIn } from '../auth/cognito'
import HomePage from './HomePage'

function renderHome() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <SceneProvider>
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/plans" element={<div>PLANS SCREEN</div>} />
          <Route path="/signup" element={<div>SIGNUP SCREEN</div>} />
          <Route path="/generating/:jobId" element={<div>GENERATING SCREEN</div>} />
        </Routes>
      </SceneProvider>
    </MemoryRouter>,
  )
}

async function openPanel() {
  const { userEvent } = await import('@testing-library/user-event')
  const user = userEvent.setup()
  await user.click(screen.getByRole('button', { name: 'words' }))
  return user
}

beforeEach(() => {
  vi.mocked(isSignedIn).mockResolvedValue(true)
  vi.mocked(getAccount).mockResolvedValue({ available: 3, frozen: 0, plan: 'free' })
  // jsdom has no object URLs; the picture path never leaves the browser and
  // only ever hands this string to the (absent-in-tests) particle cloud.
  URL.createObjectURL = vi.fn(() => 'blob:dreamscape')
  URL.revokeObjectURL = vi.fn()
})

afterEach(() => vi.restoreAllMocks())

describe('HomePage', () => {
  it('shows the credit pill for a signed-in user', async () => {
    renderHome()
    await openPanel()

    await waitFor(() => expect(screen.getByText('3 left')).toBeInTheDocument())
  })

  it('starts a generation from a mood chip and moves to the waiting screen', async () => {
    vi.mocked(startGeneration).mockResolvedValue({ job_id: 'j1', status: 'PENDING' })
    renderHome()

    const user = await openPanel()
    await user.click(screen.getByRole('button', { name: 'Stressed' }))
    await user.click(screen.getByRole('button', { name: 'Begin drifting' }))

    await waitFor(() => expect(screen.getByText('GENERATING SCREEN')).toBeInTheDocument())
    expect(startGeneration).toHaveBeenCalledWith('Stressed', 10)
  })

  it('folds the destination into the mood string', async () => {
    vi.mocked(startGeneration).mockResolvedValue({ job_id: 'j1', status: 'PENDING' })
    renderHome()

    const user = await openPanel()
    await user.click(screen.getByRole('button', { name: "Can't sleep" }))
    await user.click(screen.getByRole('button', { name: 'Ocean' }))
    await user.click(screen.getByRole('button', { name: 'Begin drifting' }))

    await waitFor(() =>
      expect(startGeneration).toHaveBeenCalledWith("Can't sleep — drifting to ocean", 10),
    )
  })

  it('accepts a mood in the user’s own words', async () => {
    vi.mocked(startGeneration).mockResolvedValue({ job_id: 'j1', status: 'PENDING' })
    renderHome()

    const user = await openPanel()
    await user.click(screen.getAllByRole('button', { name: 'In my own words…' })[0])
    await user.type(screen.getByPlaceholderText('tired but restless…'), 'tired but restless')
    await user.click(screen.getByRole('button', { name: 'Begin drifting' }))

    await waitFor(() => expect(startGeneration).toHaveBeenCalledWith('tired but restless', 10))
  })

  it('routes 402 to the plans screen instead of showing an error', async () => {
    vi.mocked(startGeneration).mockRejectedValue(new ApiError(402, 'No generations remaining.'))
    renderHome()

    const user = await openPanel()
    await user.click(screen.getByRole('button', { name: 'Anxious' }))
    await user.click(screen.getByRole('button', { name: 'Begin drifting' }))

    await waitFor(() => expect(screen.getByText('PLANS SCREEN')).toBeInTheDocument())
  })

  it('surfaces 429 as an in-progress message and stays put', async () => {
    vi.mocked(startGeneration).mockRejectedValue(new ApiError(429, 'Already in progress.'))
    renderHome()

    const user = await openPanel()
    await user.click(screen.getByRole('button', { name: 'Anxious' }))
    await user.click(screen.getByRole('button', { name: 'Begin drifting' }))

    await waitFor(() => expect(screen.getByText(/already being created/i)).toBeInTheDocument())
  })

  it('lets any user drift from a picture — no gate on the way in', async () => {
    vi.mocked(startGeneration).mockResolvedValue({ job_id: 'j1', status: 'PENDING' })
    const { container } = renderHome()
    const { userEvent } = await import('@testing-library/user-event')
    const user = userEvent.setup()

    await user.click(screen.getByRole('button', { name: 'a picture' }))
    expect(screen.getByText('Let a picture become your dreamscape')).toBeInTheDocument()

    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, {
      target: { files: [new File(['x'], 'sunset.png', { type: 'image/png' })] },
    })

    // Straight onto the mood panel, dreamscape kept — no Plus wall, no
    // fabricated keywords screen.
    expect(await screen.findByText('Choose another picture')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Low' }))
    await user.click(screen.getByRole('button', { name: 'Begin drifting' }))

    await waitFor(() => expect(screen.getByText('GENERATING SCREEN')).toBeInTheDocument())
    expect(startGeneration).toHaveBeenCalledWith('Low', 10)
  })

  it('sends an unauthenticated user to signup on Begin', async () => {
    vi.mocked(isSignedIn).mockResolvedValue(false)
    renderHome()

    const user = await openPanel()
    await user.click(screen.getByRole('button', { name: 'Restless' }))
    await user.click(screen.getByRole('button', { name: 'Begin drifting' }))

    await waitFor(() => expect(screen.getByText('SIGNUP SCREEN')).toBeInTheDocument())
  })
})
