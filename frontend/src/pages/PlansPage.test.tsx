/**
 * Plans: the Pro card is first and `?plan=plan_pro` preselects it -- that is
 * how the companion's locked entry and gate land here. Checkout is Stripe's;
 * the page only asks for the URL.
 */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, getAccount: vi.fn(), createCheckout: vi.fn() }
})

import { createCheckout, getAccount } from '../api/client'
import PlansPage from './PlansPage'

function renderPlans(search = '') {
  return render(
    <MemoryRouter initialEntries={[`/plans${search}`]}>
      <Routes>
        <Route path="/plans" element={<PlansPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getAccount).mockResolvedValue({ available: 3, frozen: 0, plan: 'free' })
  vi.mocked(createCheckout).mockResolvedValue({
    checkout_url: 'https://checkout.stripe.test/x',
    product_key: 'plan_pro',
  })
})
afterEach(() => vi.restoreAllMocks())

describe('PlansPage', () => {
  it('shows the Pro card first with its two points', () => {
    renderPlans()

    const cards = screen.getAllByRole('button').filter((b) => b.textContent?.includes('$'))
    expect(cards[0]).toHaveTextContent('Pro')
    expect(cards[0]).toHaveTextContent('$19/mo')
    expect(cards[0]).toHaveTextContent('20 meditations a month')
    expect(cards[0]).toHaveTextContent('Companion — it remembers what helps you')
    // The 10-pack stays the default selection.
    expect(screen.getByText('Get 10 sessions · $4')).toBeInTheDocument()
  })

  it('preselects Pro from ?plan=plan_pro and checks out with that key', async () => {
    const assign = vi.fn()
    Object.defineProperty(window, 'location', { value: { assign }, configurable: true })
    renderPlans('?plan=plan_pro')

    const cta = screen.getByText('Go Pro · $19/mo')
    await act(async () => {
      fireEvent.click(cta)
    })

    expect(createCheckout).toHaveBeenCalledWith('plan_pro')
    expect(assign).toHaveBeenCalledWith('https://checkout.stripe.test/x')
  })

  it('ignores an unknown ?plan', () => {
    renderPlans('?plan=plan_gold')

    expect(screen.getByText('Get 10 sessions · $4')).toBeInTheDocument()
  })
})
