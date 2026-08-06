/**
 * The generation screen's decision tree: 402 routes to plans, 429 shows the
 * in-flight message — the two API behaviours the UI must translate correctly.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

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
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/plans" element={<div>PLANS SCREEN</div>} />
        <Route path="/signup" element={<div>SIGNUP SCREEN</div>} />
        <Route path="/generating/:jobId" element={<div>GENERATING SCREEN</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.mocked(isSignedIn).mockResolvedValue(true)
  vi.mocked(getAccount).mockResolvedValue({ available: 3, frozen: 0, plan: 'free' })
})

afterEach(() => vi.restoreAllMocks())

describe('HomePage', () => {
  it('shows the credit pill for a signed-in user', async () => {
    renderHome()

    await waitFor(() => expect(screen.getByText('3 left')).toBeInTheDocument())
  })

  it('starts a generation and moves to the waiting screen', async () => {
    vi.mocked(startGeneration).mockResolvedValue({ job_id: 'j1', status: 'PENDING' })
    renderHome()

    const { userEvent } = await import('@testing-library/user-event')
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText('tired but restless…'), 'anxious')
    await user.click(screen.getByRole('button', { name: 'Begin' }))

    await waitFor(() => expect(screen.getByText('GENERATING SCREEN')).toBeInTheDocument())
    expect(startGeneration).toHaveBeenCalledWith('anxious', 10)
  })

  it('routes 402 to the plans screen instead of showing an error', async () => {
    vi.mocked(startGeneration).mockRejectedValue(new ApiError(402, 'No generations remaining.'))
    renderHome()

    const { userEvent } = await import('@testing-library/user-event')
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText('tired but restless…'), 'anxious')
    await user.click(screen.getByRole('button', { name: 'Begin' }))

    await waitFor(() => expect(screen.getByText('PLANS SCREEN')).toBeInTheDocument())
  })

  it('surfaces 429 as an in-progress message and stays put', async () => {
    vi.mocked(startGeneration).mockRejectedValue(new ApiError(429, 'Already in progress.'))
    renderHome()

    const { userEvent } = await import('@testing-library/user-event')
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText('tired but restless…'), 'anxious')
    await user.click(screen.getByRole('button', { name: 'Begin' }))

    await waitFor(() => expect(screen.getByText(/already being created/i)).toBeInTheDocument())
  })

  it('sends an unauthenticated user to signup on Begin', async () => {
    vi.mocked(isSignedIn).mockResolvedValue(false)
    renderHome()

    const { userEvent } = await import('@testing-library/user-event')
    const user = userEvent.setup()
    await user.type(screen.getByPlaceholderText('tired but restless…'), 'anxious')
    await user.click(screen.getByRole('button', { name: 'Begin' }))

    await waitFor(() => expect(screen.getByText('SIGNUP SCREEN')).toBeInTheDocument())
  })
})
