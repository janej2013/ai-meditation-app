/**
 * The account page's companion section: only a Pro account has one, it
 * lists what the companion remembers with short dates, and "Forget
 * everything" goes through the confirmation sheet before it clears.
 */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, getAccount: vi.fn() }
})
vi.mock('../api/agent', () => ({ getMemory: vi.fn(), clearMemory: vi.fn() }))
vi.mock('../auth/cognito', () => ({ currentEmail: vi.fn(), signOut: vi.fn() }))
vi.mock('../dreamscapes/useDreamscapes', () => ({ invalidateDreamCount: vi.fn() }))

import { clearMemory, getMemory } from '../api/agent'
import { getAccount } from '../api/client'
import { currentEmail } from '../auth/cognito'
import AccountPage from './AccountPage'

function renderAccount() {
  return render(
    <MemoryRouter initialEntries={['/account']}>
      <Routes>
        <Route path="/account" element={<AccountPage />} />
        <Route path="/plans" element={<div>PLANS SCREEN</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(currentEmail).mockResolvedValue('someone@example.com')
  vi.mocked(getAccount).mockResolvedValue({ available: 12, frozen: 0, plan: 'pro' })
  vi.mocked(getMemory).mockResolvedValue({
    insights: [
      { text: 'Prefers slow narration', created_at: '2026-08-26T09:00:00+00:00' },
      { text: 'Dislikes ocean sounds', created_at: '2026-08-12T09:00:00+00:00' },
    ],
    sessions_this_month: 4,
    sessions_per_month: 30,
  })
  vi.mocked(clearMemory).mockResolvedValue(undefined)
})
afterEach(() => vi.restoreAllMocks())

describe('AccountPage · What it remembers', () => {
  it('lists the insights with short dates and the month count', async () => {
    renderAccount()

    expect(await screen.findByText('Prefers slow narration')).toBeInTheDocument()
    expect(screen.getByText('Dislikes ocean sounds')).toBeInTheDocument()
    expect(screen.getByText('26 Aug')).toBeInTheDocument()
    expect(screen.getByText('12 Aug')).toBeInTheDocument()
    expect(screen.getByText('4 of 30 conversations this month')).toBeInTheDocument()
  })

  it('says Nothing yet. when there is nothing, and hides Forget', async () => {
    vi.mocked(getMemory).mockResolvedValue({
      insights: [],
      sessions_this_month: 0,
      sessions_per_month: 30,
    })
    renderAccount()

    expect(await screen.findByText('Nothing yet.')).toBeInTheDocument()
    expect(screen.queryByText('Forget everything')).not.toBeInTheDocument()
  })

  it('is absent for a free account, without asking the runner', async () => {
    vi.mocked(getAccount).mockResolvedValue({ available: 1, frozen: 0, plan: 'free' })
    renderAccount()

    expect(await screen.findByText('someone@example.com')).toBeInTheDocument()
    expect(screen.queryByText('What it remembers')).not.toBeInTheDocument()
    expect(getMemory).not.toHaveBeenCalled()
  })

  it('Forget everything asks first; Keep changes nothing', async () => {
    renderAccount()
    await screen.findByText('Prefers slow narration')

    fireEvent.click(screen.getByText('Forget everything'))
    expect(screen.getByRole('dialog', { name: 'Forget everything it remembers?' })).toBeInTheDocument()
    fireEvent.click(screen.getByText('Keep'))

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(clearMemory).not.toHaveBeenCalled()
    expect(screen.getByText('Prefers slow narration')).toBeInTheDocument()
  })

  it('Forget clears the list', async () => {
    renderAccount()
    await screen.findByText('Prefers slow narration')

    fireEvent.click(screen.getByText('Forget everything'))
    await act(async () => {
      fireEvent.click(screen.getByText('Forget'))
    })

    expect(clearMemory).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByText('Prefers slow narration')).not.toBeInTheDocument()
    expect(screen.getByText('Nothing yet.')).toBeInTheDocument()
  })
})
