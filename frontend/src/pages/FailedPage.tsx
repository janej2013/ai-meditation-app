/** The refund screen: generation failed, credit returned automatically. */
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { getAccount } from '../api/client'

export default function FailedPage() {
  const navigate = useNavigate()
  const [creditsLabel, setCreditsLabel] = useState('refunded')

  useEffect(() => {
    // The rollback already ran server-side; this just shows the fresh count.
    getAccount()
      .then((a) => setCreditsLabel(`${a.available} credit${a.available === 1 ? '' : 's'} left`))
      .catch(() => setCreditsLabel('refunded'))
  }, [])

  return (
    <div className="screen">
      <div style={{ marginTop: 120, display: 'flex', justifyContent: 'center' }}>
        <div
          style={{
            width: 96,
            height: 96,
            borderRadius: '50%',
            background: 'var(--bg-chip)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            font: '400 30px var(--font-sans)',
            color: 'var(--accent)',
          }}
        >
          !
        </div>
      </div>
      <div
        style={{
          marginTop: 38,
          textAlign: 'center',
          font: '400 25px/1.4 var(--font-sans)',
          color: 'var(--text-primary)',
        }}
      >
        That session
        <br />
        didn&rsquo;t finish
      </div>
      <div
        style={{
          marginTop: 16,
          textAlign: 'center',
          font: '400 14px/1.6 var(--font-sans)',
          color: 'oklch(0.74 0.01 60)',
        }}
      >
        Something interrupted the generation. Nothing was charged.
      </div>
      <div
        style={{
          marginTop: 24,
          borderRadius: 16,
          padding: '16px 18px',
          background: 'var(--bg-raised)',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          font: '400 13px var(--font-sans)',
          color: 'oklch(0.82 0.01 60)',
        }}
      >
        <span>Credit refunded</span>
        <span style={{ font: '500 13px var(--font-mono)', color: 'var(--accent-bright)' }}>
          {creditsLabel}
        </span>
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
        <button className="btn-primary" onClick={() => navigate('/')}>
          Try again
        </button>
        <button className="btn-ghost" onClick={() => navigate('/')}>
          Back home
        </button>
      </div>
    </div>
  )
}
