/**
 * The waiting screen: breathing circle + rotating captions while the pipeline
 * runs, polling GET /jobs/{id}. DONE carries the signed audio_url straight to
 * the player; FAILED goes to the refund screen.
 */
import { useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { pollJob } from '../api/client'

const CAPTIONS = ['Creating your meditation…', 'Breathe in…', 'And release…']

export default function GeneratingPage() {
  const { jobId } = useParams<{ jobId: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const duration = (location.state as { duration?: number } | null)?.duration ?? 10

  const [capIdx, setCapIdx] = useState(0)
  const [capOpacity, setCapOpacity] = useState(1)
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    const t = setInterval(() => {
      setCapOpacity(0)
      setTimeout(() => {
        setCapIdx((i) => (i + 1) % CAPTIONS.length)
        setCapOpacity(1)
      }, 600)
    }, 4000)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    if (!jobId) return
    const controller = new AbortController()
    abortRef.current = controller

    pollJob(jobId, { signal: controller.signal })
      .then((job) => {
        if (job.status === 'DONE' && job.audio_url) {
          navigate(`/player/${jobId}`, {
            state: { audioUrl: job.audio_url, duration },
            replace: true,
          })
        } else {
          navigate('/failed', { replace: true })
        }
      })
      .catch((e: unknown) => {
        if ((e as Error).name !== 'AbortError') navigate('/failed', { replace: true })
      })

    return () => controller.abort()
  }, [jobId, navigate, duration])

  return (
    <div className="screen" style={{ background: 'var(--wash-top)' }}>
      <div style={{ marginTop: 54, display: 'flex', justifyContent: 'center' }}>
        <div
          style={{
            width: 212,
            height: 212,
            borderRadius: '50%',
            background: 'var(--accent-soft-06)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          <div
            style={{
              width: 152,
              height: 152,
              borderRadius: '50%',
              background: 'var(--accent-soft-10)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            <div
              style={{
                width: 98,
                height: 98,
                borderRadius: '50%',
                background: 'var(--accent-soft-22)',
                animation: 'breathe10 10s cubic-bezier(.37,0,.63,1) infinite',
              }}
            />
          </div>
        </div>
      </div>

      <div
        style={{
          marginTop: 56,
          minHeight: 60,
          textAlign: 'center',
          font: '300 21px/1.5 var(--font-sans)',
          color: 'oklch(0.91 0.008 80)',
          transition: 'opacity .6s ease',
          opacity: capOpacity,
        }}
      >
        {CAPTIONS[capIdx]}
      </div>
      <div
        style={{
          marginTop: 16,
          textAlign: 'center',
          font: '400 11px var(--font-mono)',
          letterSpacing: '0.16em',
          color: 'var(--text-faint)',
        }}
      >
        BUILDING YOUR SESSION
      </div>

      <div
        style={{
          marginTop: 'auto',
          paddingBottom: 34,
          display: 'flex',
          justifyContent: 'center',
        }}
      >
        <button
          className="btn-ghost"
          onClick={() => {
            // Leaving the page only stops watching; the pipeline finishes (or
            // refunds) on its own, and the session stays available under the
            // account either way.
            abortRef.current?.abort()
            navigate('/')
          }}
        >
          Cancel
        </button>
      </div>
    </div>
  )
}
