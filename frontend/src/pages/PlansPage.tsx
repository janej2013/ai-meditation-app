/**
 * Plans / top-up. Selecting a product POSTs /billing/checkout and follows the
 * returned Stripe-hosted URL — no payment UI of our own, ever.
 *
 * Product keys must match backend/api/products.py; the price shown is display
 * copy, the amount charged is whatever the Stripe price object says. (The
 * prototype's "Unlimited" tier stays aspirational — the real monthly product
 * grants 20 sessions, and the copy says so.)
 */
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { createCheckout, getAccount } from '../api/client'

const PLANS = [
  {
    key: 'pack_10',
    label: '10 sessions',
    note: 'one-time · never expires',
    price: '$4',
    cta: 'Get 10 sessions · $4',
  },
  {
    key: 'plan_monthly',
    label: 'Monthly',
    note: '20 sessions a month · cancel anytime',
    price: '$9/mo',
    cta: 'Go monthly · $9/mo',
  },
]

export default function PlansPage() {
  const navigate = useNavigate()
  const [selected, setSelected] = useState(PLANS[0].key)
  const [credits, setCredits] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getAccount()
      .then((a) => setCredits(a.available))
      .catch(() => setCredits(null))
  }, [])

  const plan = PLANS.find((p) => p.key === selected) ?? PLANS[0]
  const out = credits !== null && credits < 1

  const upgrade = async () => {
    setBusy(true)
    setError(null)
    try {
      const { checkout_url } = await createCheckout(plan.key)
      // Stripe hosts the whole payment flow; success/cancel URLs bring the
      // user back to /billing/success, which refreshes the balance.
      window.location.assign(checkout_url)
    } catch {
      setError('Could not start checkout. Please try again.')
      setBusy(false)
    }
  }

  return (
    <div className="screen" style={{ background: 'var(--wash-plans)' }}>
      <div style={{ marginTop: 20 }}>
        <button className="btn-back" onClick={() => navigate('/')}>
          ← Home
        </button>
      </div>
      <div
        style={{
          marginTop: 60,
          font: '400 27px/1.35 var(--font-sans)',
          color: 'var(--text-primary)',
        }}
      >
        {out ? "You're out of credits" : 'Top up credits'}
      </div>
      <div
        style={{
          marginTop: 14,
          font: '400 14px/1.6 var(--font-sans)',
          color: 'var(--text-body)',
        }}
      >
        {out
          ? 'Each session costs one credit. Top up to keep going — nothing expires.'
          : credits !== null
            ? `You have ${credits} left. Add more now so you never run out mid-week — nothing expires.`
            : 'Each session costs one credit — nothing expires.'}
      </div>

      <div style={{ marginTop: 30, display: 'flex', flexDirection: 'column', gap: 11 }}>
        {PLANS.map((p) => (
          <button
            key={p.key}
            onClick={() => setSelected(p.key)}
            style={{
              textAlign: 'left',
              border: 'none',
              borderRadius: 18,
              padding: '18px 20px',
              cursor: 'pointer',
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              background: p.key === selected ? 'var(--bg-plan-selected)' : 'var(--bg-plan)',
              boxShadow: p.key === selected ? 'inset 0 0 0 1.5px var(--accent)' : 'none',
            }}
          >
            <span style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
              <span style={{ font: '400 15px var(--font-sans)', color: 'var(--text-primary)' }}>
                {p.label}
              </span>
              <span style={{ font: '400 12px var(--font-sans)', color: 'var(--text-hint)' }}>
                {p.note}
              </span>
            </span>
            <span style={{ font: '500 15px var(--font-mono)', color: 'var(--accent-plus)' }}>
              {p.price}
            </span>
          </button>
        ))}
      </div>

      {error && (
        <div className="error-text" style={{ marginTop: 16 }}>
          {error}
        </div>
      )}

      <div
        style={{
          marginTop: 'auto',
          paddingBottom: 34,
          display: 'flex',
          flexDirection: 'column',
          gap: 14,
        }}
      >
        <button className="btn-accent" onClick={() => void upgrade()} disabled={busy}>
          {busy ? 'Opening checkout…' : plan.cta}
        </button>
        <button className="btn-ghost" onClick={() => navigate('/')}>
          {out ? 'Maybe later' : 'Not now'}
        </button>
      </div>
    </div>
  )
}
