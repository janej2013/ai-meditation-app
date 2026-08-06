/**
 * Email confirmation: the six-digit Cognito code. On success the user is
 * signed in with the password captured at signup, and — if they arrived here
 * mid-generate — sent back home to continue.
 */
import { useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { confirmSignUp, resendCode, signIn } from '../auth/cognito'

export default function VerifyPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const state = location.state as { email?: string; password?: string; resume?: boolean } | null
  const email = state?.email ?? ''

  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [resent, setResent] = useState(false)

  const codeOk = /^\d{6}$/.test(code)

  const verify = async () => {
    if (!codeOk || busy || !email) return
    setBusy(true)
    setError(null)
    try {
      await confirmSignUp(email, code)
      // The post-confirmation trigger grants the free credit server-side.
      if (state?.password) await signIn(email, state.password)
      navigate(state?.resume ? '/' : '/account', { replace: true })
    } catch (e) {
      const err = e as Error & { code?: string }
      setError(
        err.code === 'CodeMismatchException'
          ? "That code doesn't match."
          : err.code === 'ExpiredCodeException'
            ? 'That code expired — resend and try again.'
            : 'Verification failed. Please try again.',
      )
    } finally {
      setBusy(false)
    }
  }

  if (!email) {
    navigate('/signup', { replace: true })
    return null
  }

  return (
    <div className="screen">
      <div style={{ marginTop: 20 }}>
        <button className="btn-back" onClick={() => navigate('/signup')}>
          ← Back
        </button>
      </div>
      <div
        style={{
          marginTop: 64,
          font: '400 27px/1.35 var(--font-sans)',
          color: 'var(--text-primary)',
        }}
      >
        Enter the code
      </div>
      <div
        style={{
          marginTop: 14,
          font: '400 14px/1.6 var(--font-sans)',
          color: 'oklch(0.74 0.01 60)',
        }}
      >
        Sent to {email}.
      </div>
      <input
        className="text-input"
        style={{
          marginTop: 28,
          padding: '20px 18px',
          fontFamily: 'var(--font-mono)',
          fontSize: 26,
          letterSpacing: '0.42em',
          textAlign: 'center',
        }}
        inputMode="numeric"
        autoComplete="one-time-code"
        value={code}
        onChange={(e) => {
          setCode(e.target.value.replace(/\D/g, '').slice(0, 6))
          setError(null)
        }}
        placeholder="––––––"
      />
      {error && (
        <div className="error-text" style={{ marginTop: 12, textAlign: 'center' }}>
          {error}
        </div>
      )}
      <div style={{ marginTop: 20, textAlign: 'center' }}>
        <button
          className="btn-ghost"
          style={{ color: 'var(--accent-bright)', fontSize: 12.5 }}
          onClick={() => {
            void resendCode(email).then(() => setResent(true))
          }}
        >
          {resent ? 'Code resent' : 'Resend code'}
        </button>
      </div>
      <div style={{ marginTop: 'auto', paddingBottom: 34 }}>
        <button
          className="btn-primary"
          style={{ width: '100%' }}
          onClick={() => void verify()}
          disabled={!codeOk || busy}
        >
          {busy ? '…' : 'Verify'}
        </button>
      </div>
    </div>
  )
}
