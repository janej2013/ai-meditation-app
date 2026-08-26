/** Account: balance, plan, buy-more, sign out. GET /account is the source. */
import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { clearMemory, getMemory, type Memory } from '../api/agent'
import { NotSignedInError, getAccount, type Account } from '../api/client'
import { currentEmail, signOut } from '../auth/cognito'
import { invalidateDreamCount } from '../dreamscapes/useDreamscapes'

export default function AccountPage() {
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const [account, setAccount] = useState<Account | null>(null)
  const [email, setEmail] = useState('')
  // What the companion remembers -- only a Pro account has a companion.
  const [memory, setMemory] = useState<Memory | null>(null)
  const [forgetting, setForgetting] = useState(false)

  // `?paid=1` is the Stripe success return: the webhook has usually landed by
  // the time the redirect completes, so one fetch shows the new balance; a
  // slow webhook shows the old one until the next visit, which is acceptable.
  const justPaid = params.get('paid') === '1'

  useEffect(() => {
    void currentEmail().then((e) => setEmail(e ?? ''))
    getAccount()
      .then((a) => {
        setAccount(a)
        if (a.plan === 'pro')
          getMemory()
            .then(setMemory)
            .catch(() => setMemory(null))
      })
      .catch((e: unknown) => {
        if (e instanceof NotSignedInError) navigate('/signup', { replace: true })
      })
  }, [navigate])

  const forget = async () => {
    try {
      await clearMemory()
      setMemory((m) => (m ? { ...m, insights: [] } : m))
    } finally {
      setForgetting(false)
    }
  }
  const shortDate = (iso: string) =>
    new Date(iso).toLocaleDateString('en-AU', { day: 'numeric', month: 'short' })

  return (
    <div className="screen">
      <div style={{ marginTop: 20 }}>
        <button className="btn-back" onClick={() => navigate('/')}>
          ← Home
        </button>
      </div>
      <div className="label-mono" style={{ marginTop: 56, letterSpacing: '0.16em' }}>
        ACCOUNT
      </div>
      <div
        style={{
          marginTop: 12,
          font: '400 20px var(--font-sans)',
          color: 'var(--text-primary)',
        }}
      >
        {email || '…'}
      </div>

      {justPaid && (
        <div
          style={{
            marginTop: 18,
            borderRadius: 14,
            padding: '13px 16px',
            background: 'var(--accent-soft-11)',
            font: '400 13px var(--font-sans)',
            color: 'var(--accent-text)',
          }}
        >
          Payment received — thank you.
        </div>
      )}

      <div
        style={{
          marginTop: 24,
          borderRadius: 20,
          padding: 24,
          background: 'var(--bg-raised)',
          display: 'flex',
          flexDirection: 'column',
          gap: 6,
        }}
      >
        <div style={{ font: '400 56px/1 var(--font-mono)', color: 'var(--accent-plus)' }}>
          {account ? account.available : '–'}
        </div>
        <div style={{ font: '400 13.5px var(--font-sans)', color: 'var(--text-secondary)' }}>
          sessions left
        </div>
      </div>

      <div style={{ marginTop: 22, display: 'flex', flexDirection: 'column' }}>
        <div
          style={{
            padding: '15px 0',
            borderBottom: '1px solid var(--border-row)',
            display: 'flex',
            justifyContent: 'space-between',
            font: '400 13.5px var(--font-sans)',
            color: 'var(--text-secondary)',
          }}
        >
          <span>Plan</span>
          <span style={{ color: 'var(--text-bright)', textTransform: 'capitalize' }}>
            {account?.plan ?? '…'}
          </span>
        </div>
        <div
          style={{
            padding: '15px 0',
            borderBottom: '1px solid var(--border-row)',
            display: 'flex',
            justifyContent: 'space-between',
            font: '400 13.5px var(--font-sans)',
            color: 'var(--text-secondary)',
          }}
        >
          <span>In progress</span>
          <span style={{ color: 'var(--text-bright)' }}>
            {account ? (account.frozen > 0 ? '1 session generating' : 'none') : '…'}
          </span>
        </div>
        <div
          style={{
            padding: '15px 0',
            display: 'flex',
            justifyContent: 'space-between',
            font: '400 13.5px var(--font-sans)',
            color: 'var(--text-secondary)',
          }}
        >
          <span>Voice</span>
          <span style={{ color: 'var(--text-bright)' }}>Soft, low</span>
        </div>
      </div>

      {account?.plan === 'pro' && (
        <section className="memory-section" aria-label="What it remembers">
          <div className="memory-title">What it remembers</div>
          <div className="memory-sub">Things you've told the companion about your meditations.</div>
          {memory && (
            <div className="memory-count">
              {memory.sessions_this_month} of {memory.sessions_per_month} conversations this month
            </div>
          )}
          <div className="memory-list">
            {memory && memory.insights.length === 0 && (
              <div className="memory-empty">Nothing yet.</div>
            )}
            {memory?.insights.map((i, n) => (
              <div key={n} className="memory-row">
                <span className="memory-text">{i.text}</span>
                <span className="memory-date">{shortDate(i.created_at)}</span>
              </div>
            ))}
          </div>
          {memory && memory.insights.length > 0 && (
            <button
              className="btn-ghost"
              style={{ alignSelf: 'flex-start', minHeight: 44, padding: 0, marginTop: 4 }}
              onClick={() => setForgetting(true)}
            >
              Forget everything
            </button>
          )}
        </section>
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
        <button
          className="btn-primary"
          style={{ height: 60, borderRadius: 30, fontSize: 16 }}
          onClick={() => navigate('/plans')}
        >
          Get more credits
        </button>
        <button
          className="btn-ghost"
          onClick={() => {
            signOut()
            invalidateDreamCount()
            navigate('/')
          }}
        >
          Sign out
        </button>
        <button
          className="btn-ghost"
          style={{ color: 'var(--text-dim)', fontSize: 13, minHeight: 44 }}
          onClick={() => navigate('/privacy', { state: { from: '/account' } })}
        >
          Privacy
        </button>
      </div>

      {forgetting && (
        <div
          role="dialog"
          aria-label="Forget everything it remembers?"
          style={{
            position: 'absolute',
            inset: 0,
            zIndex: 4,
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'flex-end',
            background: 'oklch(0.145 0.024 265 / 0.6)',
            backdropFilter: 'blur(3px)',
          }}
        >
          <div
            style={{
              borderRadius: '26px 26px 0 0',
              padding: '30px 30px 34px',
              background: 'oklch(0.255 0.032 265)',
              display: 'flex',
              flexDirection: 'column',
              gap: 8,
            }}
          >
            <div style={{ font: '300 21px var(--font-sans)', color: 'var(--text-primary)' }}>
              Forget everything it remembers?
            </div>
            <div
              style={{ font: '400 13px/1.6 var(--font-sans)', color: 'oklch(0.760 0.018 275)' }}
            >
              It will start fresh next time.
            </div>
            <div style={{ marginTop: 18, display: 'flex', flexDirection: 'column', gap: 6 }}>
              <button
                onClick={() => void forget()}
                style={{
                  height: 54,
                  border: 'none',
                  borderRadius: 27,
                  background: 'var(--btn-primary-bg)',
                  fontSize: 15.5,
                  fontWeight: 500,
                  color: 'var(--btn-primary-fg)',
                  cursor: 'pointer',
                }}
              >
                Forget
              </button>
              <button
                className="btn-ghost"
                style={{ padding: '14px 0' }}
                onClick={() => setForgetting(false)}
              >
                Keep
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
