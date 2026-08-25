/** Account: balance, plan, buy-more, sign out. GET /account is the source. */
import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { NotSignedInError, getAccount, type Account } from '../api/client'
import { currentEmail, signOut } from '../auth/cognito'
import { invalidateDreamCount } from '../dreamscapes/useDreamscapes'

export default function AccountPage() {
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const [account, setAccount] = useState<Account | null>(null)
  const [email, setEmail] = useState('')

  // `?paid=1` is the Stripe success return: the webhook has usually landed by
  // the time the redirect completes, so one fetch shows the new balance; a
  // slow webhook shows the old one until the next visit, which is acceptable.
  const justPaid = params.get('paid') === '1'

  useEffect(() => {
    void currentEmail().then((e) => setEmail(e ?? ''))
    getAccount()
      .then(setAccount)
      .catch((e: unknown) => {
        if (e instanceof NotSignedInError) navigate('/signup', { replace: true })
      })
  }, [navigate])

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
      </div>
    </div>
  )
}
