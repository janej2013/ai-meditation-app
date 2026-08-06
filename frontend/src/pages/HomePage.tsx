/**
 * The generation screen — "How are you feeling?".
 * Mood input + chips + duration + Begin, per the prototype's home state.
 */
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError, NotSignedInError, getAccount, startGeneration } from '../api/client'
import { isSignedIn } from '../auth/cognito'

const CHIPS = ['anxious', 'wired', 'heavy', "can't sleep"]
const DURATIONS = [5, 10, 15]

export default function HomePage() {
  const navigate = useNavigate()
  const [feeling, setFeeling] = useState('')
  const [duration, setDuration] = useState(10)
  const [creditPill, setCreditPill] = useState('Sign in')
  const [signedIn, setSignedIn] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    void (async () => {
      if (!(await isSignedIn())) return
      setSignedIn(true)
      try {
        const account = await getAccount()
        setCreditPill(`${account.available} left`)
      } catch {
        setCreditPill('Account')
      }
    })()
  }, [])

  const begin = async () => {
    if (!feeling.trim() || busy) return
    if (!signedIn) {
      navigate('/signup', { state: { resume: true } })
      return
    }
    setBusy(true)
    setError(null)
    try {
      const { job_id } = await startGeneration(feeling.trim(), duration)
      navigate(`/generating/${job_id}`, { state: { duration } })
    } catch (e) {
      if (e instanceof NotSignedInError) {
        navigate('/signup', { state: { resume: true } })
      } else if (e instanceof ApiError && e.status === 402) {
        // Out of credits: guide to purchase rather than surface an error.
        navigate('/plans')
      } else if (e instanceof ApiError && e.status === 429) {
        setError('A session is already being created. Give it a moment.')
      } else {
        setError('Could not start. Please try again.')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="screen">
      <div
        style={{
          marginTop: 60,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}
      >
        <div
          style={{
            font: '500 11px var(--font-mono)',
            letterSpacing: '0.16em',
            color: 'var(--accent)',
          }}
        >
          TONIGHT
        </div>
        <button
          onClick={() => navigate(signedIn ? '/account' : '/signup')}
          style={{
            border: 'none',
            borderRadius: 16,
            padding: '8px 13px',
            background: 'var(--bg-chip)',
            font: '400 11.5px var(--font-mono)',
            color: 'oklch(0.82 0.01 60)',
            cursor: 'pointer',
          }}
        >
          {creditPill}
        </button>
      </div>

      <div
        style={{
          marginTop: 22,
          font: '400 30px/1.45 var(--font-sans)',
          color: 'var(--text-primary)',
        }}
      >
        How are you feeling?
      </div>
      <input
        className="text-input"
        style={{ marginTop: 20 }}
        value={feeling}
        onChange={(e) => setFeeling(e.target.value)}
        placeholder="tired but restless…"
        maxLength={500}
      />
      <div
        style={{
          marginTop: 14,
          font: '400 12.5px/1.5 var(--font-sans)',
          color: 'var(--text-muted)',
        }}
      >
        A few words is enough.
      </div>

      <div className="label-mono" style={{ marginTop: 34 }}>
        OR START FROM
      </div>
      <div style={{ marginTop: 14, display: 'flex', gap: 9, flexWrap: 'wrap' }}>
        {CHIPS.map((chip) => (
          <button
            key={chip}
            onClick={() => setFeeling(chip)}
            style={{
              flex: 'none',
              whiteSpace: 'nowrap',
              border: 'none',
              borderRadius: 20,
              padding: '11px 16px',
              fontSize: 13.5,
              color: 'oklch(0.82 0.01 60)',
              background: 'var(--bg-chip)',
              cursor: 'pointer',
            }}
          >
            {chip}
          </button>
        ))}
      </div>

      {error && (
        <div className="error-text" style={{ marginTop: 18 }}>
          {error}
        </div>
      )}

      <div
        style={{
          marginTop: 'auto',
          paddingBottom: 34,
          display: 'flex',
          flexDirection: 'column',
          gap: 18,
        }}
      >
        <div style={{ display: 'flex', gap: 9 }}>
          {DURATIONS.map((n) => (
            <button
              key={n}
              onClick={() => setDuration(n)}
              style={{
                flex: 1,
                border: 'none',
                borderRadius: 16,
                padding: '15px 0',
                fontSize: 14,
                cursor: 'pointer',
                transition: 'background .2s,color .2s',
                background: n === duration ? 'var(--accent)' : 'var(--bg-raised)',
                color: n === duration ? 'var(--btn-primary-fg)' : 'var(--text-secondary)',
              }}
            >
              {n} min
            </button>
          ))}
        </div>
        <button
          className="btn-primary"
          onClick={() => void begin()}
          disabled={!feeling.trim() || busy}
        >
          {busy ? 'Starting…' : 'Begin'}
        </button>
      </div>
    </div>
  )
}
