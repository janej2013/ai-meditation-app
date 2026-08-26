/** The sign-up screen's privacy entry: readable before there is an account. */
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

vi.mock('../auth/cognito', () => ({ signIn: vi.fn(), signUp: vi.fn() }))
vi.mock('../dreamscapes/useDreamscapes', () => ({ invalidateDreamCount: vi.fn() }))

import SignupPage from './SignupPage'

describe('SignupPage', () => {
  it('links to the privacy page, remembering where it came from', () => {
    render(
      <MemoryRouter initialEntries={['/signup']}>
        <Routes>
          <Route path="/signup" element={<SignupPage />} />
          <Route path="/privacy" element={<div>PRIVACY SCREEN</div>} />
        </Routes>
      </MemoryRouter>,
    )

    fireEvent.click(screen.getByText('Privacy'))

    expect(screen.getByText('PRIVACY SCREEN')).toBeInTheDocument()
  })
})
