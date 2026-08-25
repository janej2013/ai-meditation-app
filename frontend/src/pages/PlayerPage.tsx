/**
 * The player: narration + BGM mixed live in the browser (Web Audio).
 * Progress ring, scrubber, pause/resume, BGM track switching and volume —
 * all without touching the narration source, per CLAUDE.md.
 *
 * The particle cloud behind the screen follows playback: pulsing while audio
 * runs, near-still while paused (SceneContext → SceneLayer, per prototype).
 */
import { useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { NotSignedInError, getJob } from '../api/client'
import { BGM_TRACKS, DEFAULT_BGM_VOLUME, bgmUrl, mixer } from '../audio/mixer'
import { dreamTitleLines } from '../dreamscapes/title'
import { wovenAgo } from '../dreamscapes/wovenAgo'
import { useFadeIn } from '../hooks/useFadeIn'
import { useScene } from '../scene/SceneContext'

function fmt(s: number): string {
  const m = Math.floor(s / 60)
  const sec = Math.floor(s % 60)
  return `${m}:${String(sec).padStart(2, '0')}`
}

export default function PlayerPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const { jobId } = useParams<{ jobId: string }>()
  const { setPlaying: setScenePlaying, setCloudSrc, setDissolve } = useScene()
  // The handoff from GeneratingPage, when there was one. It does not survive a
  // reload, and its signature expires in 15 minutes -- so it is an
  // optimisation, and the job id in the path is what actually addresses the
  // session.
  const state = location.state as {
    audioUrl?: string
    feeling?: string
    destination?: string
    pic?: boolean
    from?: 'dreamscapes'
    keywords?: string[] | null
    moodExcerpt?: string | null
    createdAt?: string | null
  } | null
  const handoffUrl = state?.audioUrl

  // "Stressed, can't sleep / quiet mind" — the mood carried over from home,
  // with the prototype's words-mode second line as the resting fallback. A
  // reload arrives without state and gets the plain title.
  const feeling = state?.feeling
  const destination = state?.destination
  // Revisiting from the collection: the prototype's header variant — kicker
  // REVISITING, a woven-ago line, and the keywords as the title.
  const revisiting = state?.from === 'dreamscapes'
  const titleLines = revisiting
    ? dreamTitleLines(state?.keywords, state?.moodExcerpt)
    : feeling
      ? [
          `${feeling.charAt(0).toUpperCase()}${feeling.slice(1)},`,
          destination && destination !== 'Anywhere' ? destination.toLowerCase() : 'quiet mind',
        ]
      : ['Your session']

  // The shared mixer is only ever touched from effects and event handlers --
  // never during render, which the react-hooks rules (correctly) forbid for
  // mutable objects. It may already be playing the ambient track that started
  // on the home screen; loading the narration leaves that running.
  const [ready, setReady] = useState(false)
  const [failed, setFailed] = useState(false)
  const [playing, setPlaying] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [duration, setDuration] = useState(0)
  const [trackId, setTrackId] = useState(BGM_TRACKS[0].id)
  const [bgmVolume, setBgmVolume] = useState(DEFAULT_BGM_VOLUME)
  const fadeIn = useFadeIn() // the prototype's playFade: breathe in on arrival
  const trackRef = useRef<HTMLDivElement>(null)

  // The cloud pulses with playback; leaving the player releases it.
  useEffect(() => {
    setScenePlaying(playing)
    return () => setScenePlaying(null)
  }, [playing, setScenePlaying])

  // Load both tracks, then wait for the listener to press play.
  useEffect(() => {
    if (!jobId) {
      navigate('/', { replace: true })
      return
    }
    let cancelled = false

    /** A signature minted just now, for a job that is already DONE. */
    const freshNarrationUrl = async (): Promise<string> => {
      const job = await getJob(jobId)
      if (!job.audio_url) throw new Error(`job ${jobId} has no audio`)
      // A revisited picture dreamscape gets its own picture back behind the
      // player: the signed upload, sampled into the cloud fully dissolved.
      // The handoff path never comes through here -- its cloud is already
      // the local blob from the home screen.
      if (job.picture_url && !cancelled) {
        setCloudSrc(job.picture_url)
        setDissolve(1)
      }
      return job.audio_url
    }

    const loadNarration = async (): Promise<void> => {
      if (handoffUrl) {
        try {
          await mixer.loadNarration(handoffUrl)
          return
        } catch {
          // Expired while the tab sat idle, most likely. Fall through and ask
          // the API to sign a new one rather than stranding a paid session.
        }
      }
      await mixer.loadNarration(await freshNarrationUrl())
    }

    /** Best-effort: a missing or unfetchable BGM object must never take the
     * paid narration down with it -- the session just plays voice-only. */
    const loadBgmQuietly = async (): Promise<void> => {
      const initial = BGM_TRACKS.find((t) => t.id === trackId) ?? BGM_TRACKS[0]
      const url = bgmUrl(initial)
      if (!url) return
      try {
        await mixer.loadBgm(url)
      } catch {
        // e.g. `make upload-bgm` was never run against this environment.
      }
    }

    void (async () => {
      try {
        // Independent downloads; decoding them in parallel halves time-to-ready.
        await Promise.all([loadNarration(), loadBgmQuietly()])
        if (!cancelled) {
          setDuration(mixer.duration())
          setReady(true)
        }
      } catch (e) {
        if (cancelled) return
        // A dead session token is not a dead session; send them to sign in
        // rather than telling them the recording is gone.
        if (e instanceof NotSignedInError) navigate('/signup', { replace: true })
        else setFailed(true)
      }
    })()
    mixer.onEnded = () => setPlaying(false)
    return () => {
      cancelled = true
      mixer.onEnded = null
      // Leaving the player ends the session: narration and music both stop.
      // The picture behind it is SceneLayer's to clear, by route.
      mixer.endSession()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount-only load
  }, [jobId])

  useEffect(() => {
    const t = setInterval(() => setElapsed(mixer.elapsed()), 500)
    return () => clearInterval(t)
  }, [])

  const togglePlay = async () => {
    if (!ready) return
    if (mixer.isPlaying()) {
      mixer.pause()
      setPlaying(false)
    } else {
      await mixer.play()
      setPlaying(true)
    }
  }

  const seek = async (e: React.MouseEvent) => {
    const el = trackRef.current
    if (!el || !ready) return
    const r = el.getBoundingClientRect()
    const p = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width))
    await mixer.seek(p * duration)
    setElapsed(mixer.elapsed())
  }

  const switchTrack = async (id: string) => {
    setTrackId(id)
    const track = BGM_TRACKS.find((t) => t.id === id)
    // Mid-session switch: only the BGM source is replaced; narration runs on.
    await mixer.loadBgm(track ? bgmUrl(track) : null)
  }

  const playPct = duration ? (elapsed / duration) * 100 : 0
  const r = 84
  const C = 2 * Math.PI * r

  if (failed) {
    return (
      <div className="screen">
        {/* No longer "the link expired" -- an expired signature is refreshed
            above. Reaching here means the session itself is unavailable. */}
        <div style={{ marginTop: 120, textAlign: 'center', color: 'var(--text-secondary)' }}>
          This session could not be loaded.
        </div>
        <div style={{ marginTop: 'auto', paddingBottom: 34 }}>
          <button
            className="btn-primary"
            style={{ width: '100%' }}
            onClick={() => navigate('/')}
          >
            Back home
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="screen" style={{ background: 'var(--wash-player)', ...fadeIn }}>
      <div
        style={{
          marginTop: 16,
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}
      >
        <button className="btn-back" onClick={() => navigate('/')}>
          ← Home
        </button>
      </div>

      <div
        style={{
          marginTop: 52,
          textAlign: 'center',
          display: 'flex',
          flexDirection: 'column',
          gap: 12,
        }}
      >
        <div
          style={{
            font: '500 10px var(--font-mono)',
            letterSpacing: '0.16em',
            color: 'var(--accent)',
          }}
        >
          {revisiting ? 'REVISITING' : 'NOW PLAYING'}
        </div>
        {revisiting && state?.createdAt && (
          <div style={{ font: '400 11.5px var(--font-mono)', color: 'oklch(0.740 0.018 275)' }}>
            {wovenAgo(new Date(state.createdAt))}
          </div>
        )}
        <div style={{ font: '400 28px/1.35 var(--font-sans)', color: 'var(--text-primary)' }}>
          {titleLines.map((line, i) => (
            <span key={line}>
              {i > 0 && <br />}
              {line}
            </span>
          ))}
        </div>
        <div style={{ font: '400 12.5px var(--font-sans)', color: 'oklch(0.86 0.016 275)' }}>
          {duration ? `${Math.round(duration / 60)} min` : '…'} ·{' '}
          {state?.pic ? 'from your picture' : 'voice + soft pad'}
        </div>
      </div>

      <div
        style={{
          marginTop: 36,
          display: 'flex',
          justifyContent: 'center',
          position: 'relative',
        }}
      >
        <svg
          width={184}
          height={184}
          viewBox="0 0 184 184"
          style={{ transform: 'rotate(-90deg)' }}
        >
          <circle
            cx={92}
            cy={92}
            r={r}
            fill="none"
            stroke="var(--border-subtle)"
            strokeWidth={2}
          />
          <circle
            cx={92}
            cy={92}
            r={r}
            fill="none"
            stroke="var(--accent)"
            strokeWidth={2.5}
            strokeLinecap="round"
            strokeDasharray={C}
            strokeDashoffset={C * (1 - playPct / 100)}
            style={{ transition: 'stroke-dashoffset .4s linear' }}
          />
        </svg>
        <div
          style={{
            position: 'absolute',
            inset: 0,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            font: '400 24px var(--font-sans)',
            color: 'oklch(0.885 0.016 275)',
          }}
        >
          {fmt(elapsed)}
        </div>
      </div>

      {/* BGM: switchable mid-session, volume independent of the voice. Not in
          the prototype's player frame, but required by the product (CLAUDE.md);
          styled with the same chip/slider language. */}
      <div style={{ marginTop: 28 }}>
        <div className="label-mono">BACKGROUND</div>
        <div style={{ marginTop: 10, display: 'flex', gap: 9 }}>
          {BGM_TRACKS.map((t) => (
            <button
              key={t.id}
              className={t.id === trackId ? 'chip selected' : 'chip'}
              style={{ padding: '10px 15px', fontSize: 13 }}
              onClick={() => void switchTrack(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>
        <input
          type="range"
          min={0}
          max={100}
          value={Math.round(bgmVolume * 100)}
          onChange={(e) => {
            const v = Number(e.target.value) / 100
            setBgmVolume(v)
            mixer.setBgmVolume(v)
          }}
          aria-label="Background volume"
          style={{ marginTop: 14, width: '100%', accentColor: 'var(--accent)' as string }}
        />
      </div>

      <div
        style={{
          marginTop: 'auto',
          paddingBottom: 38,
          display: 'flex',
          alignItems: 'center',
          gap: 16,
        }}
      >
        <button
          onClick={() => void togglePlay()}
          disabled={!ready}
          style={{
            width: 70,
            height: 70,
            flex: 'none',
            border: 'none',
            borderRadius: '50%',
            background: 'var(--btn-primary-bg)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: 16,
            color: 'var(--btn-primary-fg)',
            cursor: ready ? 'pointer' : 'default',
            opacity: ready ? 1 : 0.6,
          }}
        >
          {playing ? '❚❚' : '▶'}
        </button>
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 11 }}>
          <div
            ref={trackRef}
            onClick={(e) => void seek(e)}
            style={{ height: 22, display: 'flex', alignItems: 'center', cursor: 'pointer' }}
          >
            <div
              style={{
                width: '100%',
                height: 3,
                borderRadius: 2,
                background: 'var(--border-subtle)',
                position: 'relative',
              }}
            >
              <div
                style={{
                  height: '100%',
                  borderRadius: 2,
                  background: 'var(--accent)',
                  width: `${playPct}%`,
                }}
              />
              <div
                style={{
                  position: 'absolute',
                  top: -5.5,
                  width: 14,
                  height: 14,
                  borderRadius: '50%',
                  background: 'var(--bg-shell)',
                  border: '2px solid var(--accent)',
                  transform: 'translateX(-7px)',
                  left: `${playPct}%`,
                }}
              />
            </div>
          </div>
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              font: '400 11.5px var(--font-mono)',
              color: 'var(--text-muted)',
            }}
          >
            <span>{fmt(elapsed)}</span>
            <span>-{fmt(Math.max(0, duration - elapsed))}</span>
          </div>
        </div>
      </div>
    </div>
  )
}
