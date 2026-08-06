/**
 * Sign up / sign in. The prototype's passwordless copy ("no password to
 * remember") is aspirational — Cognito's standard flow needs a password, so
 * this screen collects email + password and the verify screen confirms the
 * emailed code. Existing users switch to the sign-in variant.
 */
import { useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { signIn, signUp } from '../auth/cognito'

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/

export default function SignupPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const resume = Boolean((location.state as { resume?: boolean } | null)?.resume)

  const [mode, setMode] = useState<'signup' | 'signin'>('signup')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const emailOk = EMAIL_RE.test(email.trim())
  const passwordOk = password.length >= 8
  const canSubmit = emailOk && passwordOk && !busy

  const submit = async () => {
    if (!canSubmit) return
    setBusy(true)
    setError(null)
    try {
      if (mode === 'signup') {
        await signUp(email.trim(), password)
        navigate('/verify', { state: { email: email.trim(), password, resume } })
      } else {
        await signIn(email.trim(), password)
        navigate(resume ? '/' : '/account')
      }
    } catch (e) {
      const err = e as Error & { code?: string }
      if (err.code === 'UsernameExistsException') {
        setMode('signin')
        setError('That email already has an account — sign in instead.')
      } else if (err.code === 'UserNotConfirmedException') {
        navigate('/verify', { state: { email: email.trim(), password, resume } })
      } else if (err.code === 'NotAuthorizedException') {
        setError('Wrong email or password.')
      } else {
        setError('Something went wrong. Please try again.')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="screen">
      <div style={{ marginTop: 20 }}>
        <button className="btn-back" onClick={() => navigate('/')}>
          ← Home
        </button>
      </div>
      <div
        style={{
          marginTop: 64,
          font: '400 27px/1.35 var(--font-sans)',
          color: 'var(--text-primary)',
        }}
      >
        {mode === 'signup' ? (
          <>
            Keep your
            <br />
            sessions
          </>
        ) : (
          'Welcome back'
        )}
      </div>
      <div
        style={{
          marginTop: 14,
          font: '400 14px/1.6 var(--font-sans)',
          color: 'var(--text-body)',
        }}
      >
        {mode === 'signup'
          ? "We'll email you a six-digit code to confirm your address."
          : 'Sign in to your sessions and credits.'}
      </div>

      <input
        className="text-input-solid"
        style={{ marginTop: 28 }}
        type="email"
        autoComplete="email"
        value={email}
        onChange={(e) => {
          setEmail(e.target.value)
          setError(null)
        }}
        placeholder="you@example.com"
      />
      <input
        className="text-input-solid"
        style={{ marginTop: 12 }}
        type="password"
        autoComplete={mode === 'signup' ? 'new-password' : 'current-password'}
        value={password}
        onChange={(e) => {
          setPassword(e.target.value)
          setError(null)
        }}
        placeholder={mode === 'signup' ? 'choose a password (8+ characters)' : 'password'}
      />
      {error && (
        <div className="error-text" style={{ marginTop: 12 }}>
          {error}
        </div>
      )}

      <div style={{ marginTop: 20, textAlign: 'center' }}>
        <button
          className="btn-ghost"
          style={{ color: 'var(--accent-text)', fontSize: 12.5 }}
          onClick={() => {
            setMode(mode === 'signup' ? 'signin' : 'signup')
            setError(null)
          }}
        >
          {mode === 'signup'
            ? 'Already have an account? Sign in'
            : 'New here? Create an account'}
        </button>
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
        <button className="btn-primary" onClick={() => void submit()} disabled={!canSubmit}>
          {busy ? '…' : mode === 'signup' ? 'Send code' : 'Sign in'}
        </button>
        <div
          style={{
            textAlign: 'center',
            font: '400 11.5px/1.6 var(--font-sans)',
            color: 'var(--text-faint)',
          }}
        >
          By continuing you agree to the terms.
        </div>
      </div>
    </div>
  )
}
