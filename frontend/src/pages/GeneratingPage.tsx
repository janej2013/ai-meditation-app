/**
 * The waiting screen: polling GET /jobs/{id} while the pipeline runs. Words
 * mode shows the breathing circle + rotating captions over its own radial
 * wash; picture mode keeps the user's dreamscape in view ("Weaving your
 * picture into a dream…" — the prototype's picMode), and once the pipeline
 * has described the picture, the keywords it found ("In your picture, we
 * found…"). DONE swaps the caption for "Your dreamscape is ready", fades the
 * screen out and hands the signed audio_url to the player; FAILED goes to the
 * refund screen.
 *
 * The background music started on the home screen keeps playing here. One
 * exit rule, enforced in the poll effect's cleanup so back-navigation and
 * cancels behave alike: unmounting stops the music unless the session was
 * handed to the player.
 */
import { useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { pollJob } from '../api/client'
import { mixer } from '../audio/mixer'
import { invalidateDreamCount } from '../dreamscapes/useDreamscapes'

const CAPTIONS = ['Creating your meditation…', 'Breathe in…', 'And release…']

export default function GeneratingPage() {
  const { jobId } = useParams<{ jobId: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const state = location.state as {
    duration?: number
    feeling?: string
    destination?: string
    pic?: boolean
    keywords?: string[] | null
  } | null
  const duration = state?.duration ?? 10
  const pic = state?.pic ?? false

  const [capIdx, setCapIdx] = useState(0)
  const [capOpacity, setCapOpacity] = useState(1)
  const [ready, setReady] = useState(false)
  const [fade, setFade] = useState(1)
  // Known before Begin now (the keywords screen); the poll only fills them
  // in for a session resumed without its handoff state.
  const [keywords, setKeywords] = useState<string[] | null>(state?.keywords ?? null)
  const abortRef = useRef<AbortController | null>(null)
  const handedOff = useRef(false)

  useEffect(() => {
    if (ready || pic) return
    const t = setInterval(() => {
      setCapOpacity(0)
      setTimeout(() => {
        setCapIdx((i) => (i + 1) % CAPTIONS.length)
        setCapOpacity(1)
      }, 600)
    }, 4000)
    return () => clearInterval(t)
  }, [ready, pic])

  useEffect(() => {
    if (!jobId) return
    const controller = new AbortController()
    abortRef.current = controller
    const timers: ReturnType<typeof setTimeout>[] = []

    pollJob(jobId, {
      signal: controller.signal,
      onUpdate: (job) => {
        if (job.picture_keywords?.length) setKeywords(job.picture_keywords)
      },
    })
      .then((job) => {
        if (job.status === 'DONE' && job.audio_url) {
          handedOff.current = true
          invalidateDreamCount() // one more dreamscape than home last counted
          // The prototype's arrival beat: caption swap, screen fade, player.
          setReady(true)
          setCapOpacity(1)
          timers.push(setTimeout(() => setFade(0), 1400))
          timers.push(
            setTimeout(
              () =>
                navigate(`/player/${jobId}`, {
                  state: { ...state, audioUrl: job.audio_url, duration },
                  replace: true,
                }),
              2400,
            ),
          )
        } else {
          navigate('/failed', { replace: true })
        }
      })
      .catch((e: unknown) => {
        if ((e as Error).name !== 'AbortError') navigate('/failed', { replace: true })
      })

    return () => {
      controller.abort()
      timers.forEach(clearTimeout)
      if (!handedOff.current) mixer.stopAmbient()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- poll once per job
  }, [jobId, navigate])

  return (
    <div
      className="screen"
      style={{
        background: pic ? 'var(--wash-generating-picture)' : 'var(--wash-generating)',
        transition: 'opacity 1s ease',
        opacity: fade,
      }}
    >
      {pic ? (
        // The dreamscape itself is the visual; leave room for it to breathe.
        <div style={{ marginTop: 236 }} />
      ) : (
        <div style={{ marginTop: 72, display: 'flex', justifyContent: 'center' }}>
          <div
            style={{
              width: 212,
              height: 212,
              borderRadius: '50%',
              background: 'var(--accent-soft-07)',
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
                background: 'var(--accent-soft-11)',
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
                  background: 'var(--accent-soft-24)',
                  animation: 'breathe10 10s cubic-bezier(.37,0,.63,1) infinite',
                }}
              />
            </div>
          </div>
        </div>
      )}

      <div
        style={{
          marginTop: 56,
          minHeight: 60,
          textAlign: 'center',
          font: '300 21px/1.5 var(--font-sans)',
          color: 'oklch(0.915 0.014 275)',
          transition: 'opacity .6s ease',
          opacity: capOpacity,
        }}
      >
        {ready
          ? 'Your dreamscape is ready'
          : keywords
            ? 'In your picture, we found…'
            : pic
              ? 'Weaving your picture into a dream…'
              : CAPTIONS[capIdx]}
      </div>
      {keywords && !ready && (
        <div
          style={{
            marginTop: 14,
            display: 'flex',
            flexWrap: 'wrap',
            justifyContent: 'center',
            gap: 9,
            padding: '0 30px',
          }}
        >
          {keywords.map((k) => (
            <span key={k} className="chip selected">
              {k}
            </span>
          ))}
        </div>
      )}
      {pic && !ready && !keywords && (
        <div
          style={{
            marginTop: 12,
            textAlign: 'center',
            font: '300 14px/1.5 var(--font-sans)',
            color: 'oklch(0.805 0.018 275)',
          }}
        >
          Watch it dissolve into stardust
        </div>
      )}
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
