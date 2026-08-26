/**
 * The privacy page: its sections, the numbers it states, where Back goes,
 * and the one rule that matters -- it may say less than docs/privacy.md,
 * never more. The gate test reads the document and checks every retention
 * figure the page shows appears there.
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import doc from '../../../docs/privacy.md?raw'
import PrivacyPage, { LAST_CHECKED, PRIVACY_CONTACT } from './PrivacyPage'

function renderPrivacy(state?: { from: string }) {
  return render(
    <MemoryRouter initialEntries={[{ pathname: '/signup' }, { pathname: '/privacy', state }]}>
      <Routes>
        <Route path="/privacy" element={<PrivacyPage />} />
        <Route path="/signup" element={<div>SIGNUP SCREEN</div>} />
        <Route path="/" element={<div>HOME SCREEN</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('PrivacyPage', () => {
  it('has the seven sections', () => {
    renderPrivacy()

    for (const heading of [
      'Your account',
      'Meditations from words',
      'Meditations from a picture',
      'The companion',
      'How long things are kept',
      'Who else sees it',
    ])
      expect(screen.getByRole('heading', { name: heading })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'What Drift keeps' })).toBeInTheDocument()
  })

  it('states the retention periods, the clear path and the crisis line', () => {
    renderPrivacy()
    const text = document.body.textContent ?? ''

    expect(text).toContain('30 days')
    expect(text).toContain('90 days')
    expect(text).toContain('365 days')
    expect(text).toContain('Forget everything')
    expect(text).toContain('13 11 14')
    expect(text).toContain(`Last checked against the code: ${LAST_CHECKED}`)
  })

  it('does not name a contact until there is one', () => {
    renderPrivacy()

    expect(PRIVACY_CONTACT).toBe('')
    expect(document.body.textContent).not.toContain('Write to')
  })

  it('Back returns to where the reader came from, or home', () => {
    renderPrivacy({ from: '/signup' })
    fireEvent.click(screen.getByText('← Back'))
    expect(screen.getByText('SIGNUP SCREEN')).toBeInTheDocument()

    document.body.innerHTML = ''
    renderPrivacy()
    fireEvent.click(screen.getByText('← Back'))
    expect(screen.getByText('HOME SCREEN')).toBeInTheDocument()
  })

  it('promises nothing the annotated document does not', () => {
    renderPrivacy()
    const text = document.body.textContent ?? ''
    const figures = Array.from(text.matchAll(/\d+ (?:days|conversations)/g), (m) => m[0])

    expect(figures.length).toBeGreaterThan(0)
    for (const figure of new Set(figures)) expect(doc).toContain(figure)
    // The date on the page is the document's sync date.
    expect(doc).toContain(LAST_CHECKED)
  })
})
