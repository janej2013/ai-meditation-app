import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, getAccount: vi.fn() }
})
vi.mock('../auth/cognito', () => ({ isSignedIn: vi.fn() }))

import { getAccount } from '../api/client'
import { isSignedIn } from '../auth/cognito'
import AccountPill from './AccountPill'

function renderPill(path = '/player/j1') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AccountPill />
      <Routes>
        <Route path="*" element={<div>SOME SCREEN</div>} />
        <Route path="/signup" element={<div>SIGNUP SCREEN</div>} />
        <Route path="/account" element={<div>ACCOUNT SCREEN</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => vi.clearAllMocks())

describe('AccountPill', () => {
  it('reads Sign in when signed out and opens the sign-in flow', async () => {
    vi.mocked(isSignedIn).mockResolvedValue(false)
    renderPill()

    await waitFor(() => expect(screen.getByText('Sign in')).toBeInTheDocument())
    await userEvent.click(screen.getByText('Sign in'))
    expect(screen.getByText('SIGNUP SCREEN')).toBeInTheDocument()
    expect(getAccount).not.toHaveBeenCalled()
  })

  it('shows the credits left when signed in and opens the account', async () => {
    vi.mocked(isSignedIn).mockResolvedValue(true)
    vi.mocked(getAccount).mockResolvedValue({ available: 3, frozen: 0, plan: 'free' })
    renderPill()

    await waitFor(() => expect(screen.getByText('3 left')).toBeInTheDocument())
    await userEvent.click(screen.getByText('3 left'))
    expect(screen.getByText('ACCOUNT SCREEN')).toBeInTheDocument()
  })

  it('does not refetch on the waiting screen, where the read would race the freeze', async () => {
    vi.mocked(isSignedIn).mockResolvedValue(true)
    vi.mocked(getAccount).mockResolvedValue({ available: 3, frozen: 0, plan: 'free' })
    renderPill('/generating/j1')

    await new Promise((r) => setTimeout(r, 30))
    expect(getAccount).not.toHaveBeenCalled()
    expect(screen.getByText('Sign in')).toBeInTheDocument() // nothing known yet
  })

  it('still offers the account when the balance cannot be read', async () => {
    vi.mocked(isSignedIn).mockResolvedValue(true)
    vi.mocked(getAccount).mockRejectedValue(new Error('offline'))
    renderPill()

    await waitFor(() => expect(screen.getByText('Account')).toBeInTheDocument())
  })
})
