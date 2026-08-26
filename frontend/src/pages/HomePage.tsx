/**
 * Home, straight from the prototype: a bare "Drift from words · or · a
 * picture" sentence over the particle cloud, opening into either the panel —
 * how are you feeling (chips or own words), where to drift to, duration,
 * Begin drifting — or the picture chooser.
 *
 * The picture path is its own brief -- no mood, no destination. Choosing a
 * picture first checks that the user is signed in with a credit in hand
 * (the vision call is spent before any credit is frozen); the file is then
 * normalised to a small JPEG (picture/prepare.ts), becomes the object URL the
 * particle cloud samples into the dreamscape, is uploaded to S3, and is
 * described by the picture state machine while the cloud dissolves. The
 * keywords screen ("In your picture, we found…") shows the reading, and only
 * its Begin starts a job -- which is when a credit is frozen.
 *
 * The words path takes one mood string; the destination is folded into it.
 */
import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  ApiError,
  NotSignedInError,
  describeUploadedPicture,
  getAccount,
  startGeneration,
  uploadPicture,
} from '../api/client'
import { DEFAULT_BGM_TRACK, bgmUrl, mixer } from '../audio/mixer'
import { isSignedIn } from '../auth/cognito'
import { useDreamCount } from '../dreamscapes/useDreamscapes'
import { prepareJpeg } from '../picture/prepare'
import { useScene } from '../scene/SceneContext'
import type { GeneratingHandoff } from './GeneratingPage'

const MOODS = ['Stressed', "Can't sleep", 'Anxious', 'Restless', 'Low', 'Just tired']
const PLACES = ['Anywhere', 'Ocean', 'Rainforest', 'Starry night', 'Fireplace']
const DURATIONS = [5, 10, 15]

/** The prototype's dissolve beat: the picture scatters over ~3.4 seconds. */
const DISSOLVE_MS = 3400

const sentenceButtonStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  padding: '9px 4px 12px',
  margin: '-9px -4px -12px',
  font: "300 21px 'DM Sans', system-ui, sans-serif",
  color: 'var(--text-hero)',
  borderBottom: '1px solid var(--accent-underline)',
  textShadow: '0 0 22px var(--accent-glow-22)',
  cursor: 'pointer',
}

export default function HomePage() {
  const navigate = useNavigate()
  const { setFocus, setCloudSrc, setDissolve, resetCloud, setHeroDim } = useScene()

  const [view, setView] = useState<'sentence' | 'picture' | 'keywords' | 'panel'>('sentence')
  const [moods, setMoods] = useState<string[]>([])
  const [moodMode, setMoodMode] = useState<'chips' | 'text'>('chips')
  const [moodText, setMoodText] = useState('')
  const [place, setPlace] = useState('Anywhere')
  const [destMode, setDestMode] = useState<'chips' | 'text'>('chips')
  const [destText, setDestText] = useState('')
  const [duration, setDuration] = useState(10)

  const [signedIn, setSignedIn] = useState(false)
  // The companion entry's two forms: open for a Pro plan, locked otherwise.
  // Unknown (not signed in, request failed) reads as locked -- the locked
  // card leads to Plans, which is the right place either way.
  const [plan, setPlan] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // The picture path's state: the upload id once S3 has it, the reading once
  // the vision step has, and how many keyword chips have faded in.
  const [pictureId, setPictureId] = useState<string | null>(null)
  const [keywords, setKeywords] = useState<string[] | null>(null)
  const [kwStage, setKwStage] = useState(0)
  const [pictureError, setPictureError] = useState<string | null>(null)
  const describeAbort = useRef<AbortController | null>(null)
  const kwTimers = useRef<ReturnType<typeof setTimeout>[]>([])

  const dreamCount = useDreamCount()
  const fileRef = useRef<HTMLInputElement>(null)
  const dissolveTimer = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    // Only whether to gate Begin behind sign-in; the balance itself lives in
    // the shell's AccountPill, which every screen shares.
    void (async () => {
      if (await isSignedIn()) {
        setSignedIn(true)
        getAccount()
          .then((a) => setPlan(a.plan))
          .catch(() => setPlan(null))
      }
    })()
  }, [])

  // Landing home starts a fresh session: release the previous picture (it was
  // only ever an object URL in this browser) and settle the cloud.
  useEffect(() => {
    resetCloud()
    return () => {
      if (dissolveTimer.current) clearInterval(dissolveTimer.current)
      // Leaving mid-read: stop the poll and the chip reveal, or they keep
      // hitting the API and setting state on a page that is gone.
      describeAbort.current?.abort()
      kwTimers.current.forEach(clearTimeout)
      setHeroDim(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount-only reset
  }, [])

  // The picture chooser rests the cloud at 0.34 (prototype heroOpacity).
  useEffect(() => {
    setHeroDim(view === 'picture' ? 0.34 : null)
  }, [view, setHeroDim])

  const pickMood = (m: string) =>
    setMoods((prev) => {
      if (prev.includes(m)) return prev.filter((x) => x !== m)
      // Two at most, newest kept — same rule as the prototype.
      return (prev.length >= 2 ? prev.slice(1) : prev).concat(m)
    })

  const clearPicture = () => {
    describeAbort.current?.abort()
    kwTimers.current.forEach(clearTimeout)
    kwTimers.current = []
    if (dissolveTimer.current) clearInterval(dissolveTimer.current)
    resetCloud()
    setPictureId(null)
    setKeywords(null)
    setKwStage(0)
    setPictureError(null)
  }

  /** The gate in front of the file dialog: the vision step spends before any
   * credit is frozen, so it is only offered to a signed-in user with one. */
  const choosePicture = async () => {
    if (!signedIn) {
      navigate('/signup', { state: { resume: true } })
      return
    }
    try {
      const account = await getAccount()
      if (account.available < 1) {
        navigate('/plans')
        return
      }
    } catch (e) {
      if (e instanceof NotSignedInError) {
        navigate('/signup', { state: { resume: true } })
        return
      }
      // Balance unreadable: let the API's own 402 decide on upload.
    }
    fileRef.current?.click()
  }

  const onFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files && e.target.files[0]
    // Clear the input now: browsers fire no change event when the same file
    // is re-picked, which would dead-end the "choose another" path.
    e.target.value = ''
    if (!f) return
    let picture: Blob
    try {
      picture = await prepareJpeg(f)
    } catch {
      // The browser could not decode it (an HEIC on a browser without HEIC
      // support, say). Stay on the chooser and say so.
      setError('That picture could not be read. Please choose a JPEG or PNG.')
      return
    }
    setError(null)
    clearPicture()
    setCloudSrc(URL.createObjectURL(picture))
    // Crisp picture first, then scattering into the dreamy cloud -- the
    // prototype's 3.4 s dissolve, running while the picture is read.
    setDissolve(0)
    const t0 = Date.now()
    dissolveTimer.current = setInterval(() => {
      const p = Math.min(1, (Date.now() - t0) / DISSOLVE_MS)
      setDissolve(p)
      if (p >= 1 && dissolveTimer.current) clearInterval(dissolveTimer.current)
    }, 60)
    setView('keywords')

    const controller = new AbortController()
    describeAbort.current = controller
    try {
      const id = await uploadPicture(picture)
      if (controller.signal.aborted) return
      setPictureId(id)
      const found = await describeUploadedPicture(id, { signal: controller.signal })
      if (controller.signal.aborted) return
      setKeywords(found)
      // The prototype's staggered reveal: 900 / 1600 / 2300 ms.
      found.forEach((_, i) => {
        kwTimers.current.push(setTimeout(() => setKwStage(i + 1), 900 + i * 700))
      })
    } catch (e) {
      if (controller.signal.aborted) return
      if (e instanceof NotSignedInError) {
        navigate('/signup', { state: { resume: true } })
      } else if (e instanceof ApiError && e.status === 402) {
        navigate('/plans')
      } else {
        setPictureError("We couldn't read that picture. Try another one.")
      }
    }
  }

  /**
   * The one way a session starts, from words or from a picture: the ambient
   * music begins inside the click (the only place a mobile browser allows),
   * the job is requested, and every refusal routes the same way.
   */
  const startSession = async (
    source: { mood: string } | { pictureId: string },
    handoff: Omit<GeneratingHandoff, 'duration'>,
  ) => {
    setBusy(true)
    setError(null)
    if (dissolveTimer.current) clearInterval(dissolveTimer.current)
    setDissolve(1)
    void mixer.startAmbient(bgmUrl(DEFAULT_BGM_TRACK))
    try {
      const { job_id } = await startGeneration(source, duration)
      navigate(`/generating/${job_id}`, { state: { duration, ...handoff } })
    } catch (e) {
      mixer.stopAmbient()
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

  const beginFromPicture = async () => {
    if (!pictureId || !keywords || busy) return
    await startSession({ pictureId }, { keywords, pic: true })
  }

  const feeling = moodMode === 'text' ? moodText.trim() : moods.join(', ')
  const moodOk = feeling.length > 0

  const begin = async () => {
    if (!moodOk || busy) return
    if (!signedIn) {
      navigate('/signup', { state: { resume: true } })
      return
    }
    const destination = destMode === 'text' ? destText.trim() : place
    const mood = (
      destination && destination !== 'Anywhere'
        ? `${feeling} — drifting to ${destination.toLowerCase()}`
        : feeling
    ).slice(0, 500)
    return startSession({ mood }, { feeling, destination, pic: false })
  }

  const onSentence = view === 'sentence'
  const onPanel = view === 'panel'

  return (
    <div style={{ flex: 1, minHeight: 0, position: 'relative' }}>
      {/* Sentence view — "Drift / from words · or · a picture". */}
      <div
        style={{
          position: 'absolute',
          inset: 0,
          padding: '0 34px',
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'center',
          transition: 'opacity .3s ease, transform .34s ease',
          opacity: onSentence ? 1 : 0,
          transform: onSentence ? 'translateY(0)' : 'translateY(-14px)',
          pointerEvents: onSentence ? 'auto' : 'none',
        }}
      >
        <div
          style={{
            font: "300 68px/1 'DM Sans', system-ui, sans-serif",
            letterSpacing: '-0.03em',
            color: 'var(--text-hero)',
            textShadow: '0 0 40px var(--accent-glow-hero)',
          }}
        >
          Drift
        </div>
        <div
          style={{
            marginTop: 26,
            display: 'flex',
            flexWrap: 'wrap',
            alignItems: 'baseline',
            gap: 9,
            font: "300 21px/1.5 'DM Sans', system-ui, sans-serif",
            color: 'var(--text-body)',
          }}
        >
          <span>from</span>
          <button
            onClick={() => {
              setFocus('idle')
              // Taking the words path drops any picture chosen earlier.
              clearPicture()
              setView('panel')
            }}
            onPointerDown={() => setFocus('lines')}
            onPointerUp={() => setFocus('idle')}
            onPointerLeave={() => setFocus('idle')}
            style={sentenceButtonStyle}
          >
            words
          </button>
          <span>·</span>
          <span>or</span>
          <span>·</span>
          <button
            onClick={() => {
              setFocus('idle')
              setView('picture')
            }}
            onPointerDown={() => setFocus('frame')}
            onPointerUp={() => setFocus('idle')}
            onPointerLeave={() => setFocus('idle')}
            style={sentenceButtonStyle}
          >
            a picture
          </button>
        </div>
        {/* The companion's entry ([Home · Companion entry]): the card for a
            Pro plan, dimmed with a lock otherwise -- never hidden, so the
            feature is discoverable. */}
        <div style={{ marginTop: 34, display: 'flex' }}>
          <button
            className={plan === 'pro' ? 'companion-entry' : 'companion-entry locked'}
            onClick={() => navigate(plan === 'pro' ? '/companion' : '/plans?plan=plan_pro')}
          >
            <span className="companion-entry-row">
              <span className="companion-entry-title">
                {plan !== 'pro' && (
                  <svg
                    width="11"
                    height="11"
                    viewBox="0 0 12 12"
                    fill="none"
                    aria-hidden
                    style={{ flex: 'none', display: 'block', color: 'oklch(0.80 0.045 285)' }}
                  >
                    <rect x="2.2" y="5.2" width="7.6" height="5.6" rx="1.4" fill="currentColor" />
                    <path
                      d="M4.1 5.2V3.9a1.9 1.9 0 0 1 3.8 0v1.3"
                      stroke="currentColor"
                      strokeWidth="1.15"
                      strokeLinecap="round"
                    />
                  </svg>
                )}
                Talk it through
              </span>
              <span className="pro-tag">PRO</span>
            </span>
            <span className="companion-entry-sub">A companion that remembers what helps you.</span>
            {plan !== 'pro' && <span className="companion-entry-lock">Part of Pro</span>}
          </button>
        </div>
        {/* The collection's entry line: absent (not a spinner) until the
            count is known, then fades in with it. */}
        <div
          style={{
            marginTop: 20,
            display: 'flex',
            transition: 'opacity .8s ease',
            opacity: dreamCount === null ? 0 : 1,
          }}
        >
          {dreamCount !== null && (
            <button className="dream-entry" onClick={() => navigate('/dreamscapes')}>
              {dreamCount === 0
                ? 'No dreamscapes yet'
                : dreamCount === 1
                  ? '1 dreamscape collected'
                  : `${dreamCount} dreamscapes collected`}
            </button>
          )}
        </div>
      </div>

      {/* Picture chooser. */}
      <div
        aria-hidden={view !== 'picture'}
        style={{
          position: 'absolute',
          inset: 0,
          padding: '0 30px',
          display: 'flex',
          flexDirection: 'column',
          transition: 'opacity .3s ease, transform .34s ease',
          opacity: view === 'picture' ? 1 : 0,
          transform: view === 'picture' ? 'translateY(0)' : 'translateY(16px)',
          pointerEvents: view === 'picture' ? 'auto' : 'none',
        }}
      >
        <div style={{ flex: 'none', marginTop: 14 }}>
          <button
            onClick={() => setView('sentence')}
            style={{
              background: 'none',
              border: 'none',
              padding: '9px 6px 9px 0',
              margin: '-9px 0',
              fontSize: 15,
              color: 'var(--text-back)',
              cursor: 'pointer',
            }}
          >
            ←
          </button>
        </div>
        <div
          style={{
            flex: 1,
            minHeight: 0,
            overflowY: 'auto',
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'center',
            gap: 16,
          }}
        >
          <div style={{ font: '400 27px/1.35 var(--font-sans)', color: 'var(--text-primary)' }}>
            Let a picture become your dreamscape
          </div>
          <div style={{ font: '400 14px/1.6 var(--font-sans)', color: 'var(--text-body)' }}>
            Choose a photo or artwork — a place, a memory, anything that moves you
          </div>
        </div>
        <div
          style={{
            flex: 'none',
            paddingTop: 16,
            paddingBottom: 34,
            display: 'flex',
            flexDirection: 'column',
            gap: 14,
          }}
        >
          <div style={{ display: 'flex', gap: 9 }}>
            {DURATIONS.map((n) => (
              <button
                key={n}
                className={n === duration ? 'seg selected' : 'seg'}
                onClick={() => setDuration(n)}
              >
                {n} min
              </button>
            ))}
          </div>
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            onChange={onFile}
            style={{ display: 'none' }}
          />
          <button className="btn-primary" onClick={() => void choosePicture()}>
            Choose a picture
          </button>
          <div
            style={{
              textAlign: 'center',
              font: '400 11.5px/1.5 var(--font-sans)',
              color: 'var(--text-soft)',
            }}
          >
            We'll read the mood of your picture and weave it into this meditation
          </div>
        </div>
      </div>

      {/* Keywords view — the prototype's "In your picture, we found…". Hidden
          from the accessibility tree when inactive: it carries its own Begin. */}
      <div
        aria-hidden={view !== 'keywords'}
        style={{
          position: 'absolute',
          inset: 0,
          padding: '0 30px',
          display: 'flex',
          flexDirection: 'column',
          transition: 'opacity .3s ease, transform .34s ease',
          opacity: view === 'keywords' ? 1 : 0,
          transform: view === 'keywords' ? 'translateY(0)' : 'translateY(16px)',
          pointerEvents: view === 'keywords' ? 'auto' : 'none',
        }}
      >
        <div style={{ flex: 'none', marginTop: 14 }}>
          <button
            onClick={() => {
              clearPicture()
              setView('picture')
            }}
            style={{
              background: 'none',
              border: 'none',
              padding: '9px 6px 9px 0',
              margin: '-9px 0',
              fontSize: 15,
              color: 'var(--text-back)',
              cursor: 'pointer',
            }}
          >
            ←
          </button>
        </div>
        <div
          style={{
            flex: 1,
            minHeight: 0,
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'center',
            gap: 22,
            margin: '0 -30px',
            padding: '0 30px',
            background:
              'linear-gradient(to bottom, oklch(0.20 0.032 265 / 0) 0%, oklch(0.20 0.032 265 / 0.58) 26%, oklch(0.20 0.032 265 / 0.78) 50%, oklch(0.20 0.032 265 / 0.72) 78%, oklch(0.20 0.032 265 / 0.30) 100%)',
          }}
        >
          <div
            style={{
              font: '300 23px/1.45 var(--font-sans)',
              color: 'var(--text-primary)',
              textShadow: '0 0 30px oklch(0.10 0.03 265 / 0.9)',
            }}
          >
            {pictureError
              ? pictureError
              : keywords
                ? 'In your picture, we found…'
                : 'Reading your picture…'}
          </div>
          {keywords && (
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
              {keywords.map((k, i) => (
                <span
                  key={k}
                  style={{
                    flex: 'none',
                    borderRadius: 20,
                    padding: '11px 17px',
                    font: '400 14.5px var(--font-sans)',
                    color: 'var(--text-primary)',
                    background: 'oklch(0.30 0.038 265 / 0.55)',
                    backdropFilter: 'blur(14px)',
                    boxShadow: 'inset 0 0 0 1px oklch(0.80 0.085 285 / 0.3)',
                    transition: 'opacity .8s ease, transform .8s ease',
                    opacity: kwStage > i ? 1 : 0,
                    transform: kwStage > i ? 'translateY(0)' : 'translateY(8px)',
                  }}
                >
                  {k}
                </span>
              ))}
            </div>
          )}
        </div>
        <div
          style={{
            flex: 'none',
            margin: '0 -30px',
            padding: '16px 30px 34px',
            display: 'flex',
            flexDirection: 'column',
            gap: 14,
            background:
              'linear-gradient(to bottom, oklch(0.20 0.032 265 / 0.30) 0%, oklch(0.20 0.032 265 / 0.80) 34%, oklch(0.20 0.032 265 / 0.94) 100%)',
          }}
        >
          <button
            className="btn-primary"
            onClick={() => void beginFromPicture()}
            disabled={!keywords || busy}
          >
            {busy ? 'Starting…' : 'Begin drifting'}
          </button>
          <button
            className="btn-ghost"
            style={{ padding: '11px 0' }}
            onClick={() => {
              clearPicture()
              setView('picture')
            }}
          >
            Choose another picture
          </button>
          {view === 'keywords' && error && (
            <div
              style={{
                textAlign: 'center',
                font: '400 12.5px var(--font-sans)',
                color: 'var(--error)',
              }}
            >
              {error}
            </div>
          )}
        </div>
      </div>

      {/* Panel view — the actual generation form. */}
      <div
        aria-hidden={!onPanel}
        style={{
          position: 'absolute',
          inset: 0,
          display: 'flex',
          flexDirection: 'column',
          transition: 'opacity .3s ease, transform .34s ease',
          opacity: onPanel ? 1 : 0,
          transform: onPanel ? 'translateY(0)' : 'translateY(16px)',
          pointerEvents: onPanel ? 'auto' : 'none',
        }}
      >
        <div
          style={{
            flex: 'none',
            padding: '14px 30px 0',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
          }}
        >
          <button
            onClick={() => setView('sentence')}
            style={{
              background: 'none',
              border: 'none',
              padding: 0,
              fontSize: 15,
              color: 'var(--text-back)',
              cursor: 'pointer',
            }}
          >
            ←
          </button>
        </div>

        <div
          style={{
            flex: 1,
            minHeight: 0,
            overflowY: 'auto',
            padding: '26px 30px 0',
            display: 'flex',
            flexDirection: 'column',
            gap: 26,
          }}
        >
          <div style={{ display: 'flex', flexDirection: 'column', gap: 13 }}>
            <div
              style={{ font: '400 19px/1.4 var(--font-sans)', color: 'var(--text-primary)' }}
            >
              How are you feeling right now?
            </div>
            {moodMode === 'chips' ? (
              <>
                <div style={{ display: 'flex', gap: 9, flexWrap: 'wrap' }}>
                  {MOODS.map((m) => (
                    <button
                      key={m}
                      className={moods.includes(m) ? 'chip selected' : 'chip'}
                      onClick={() => pickMood(m)}
                    >
                      {m}
                    </button>
                  ))}
                  <button className="chip-dashed" onClick={() => setMoodMode('text')}>
                    In my own words…
                  </button>
                </div>
                <div style={{ font: '400 11.5px var(--font-mono)', color: 'var(--text-soft)' }}>
                  {moods.length >= 2 ? "two is the most we'll take" : 'pick one or two'}
                </div>
              </>
            ) : (
              <>
                <input
                  className="text-input"
                  value={moodText}
                  onChange={(e) => setMoodText(e.target.value)}
                  placeholder="tired but restless…"
                  maxLength={400}
                />
                <button
                  onClick={() => setMoodMode('chips')}
                  style={{
                    alignSelf: 'flex-start',
                    background: 'none',
                    border: 'none',
                    padding: 0,
                    font: '400 11.5px var(--font-mono)',
                    color: 'var(--accent-text)',
                    cursor: 'pointer',
                  }}
                >
                  use the chips instead
                </button>
              </>
            )}
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 13 }}>
            <div
              style={{ font: '400 19px/1.4 var(--font-sans)', color: 'var(--text-primary)' }}
            >
              Where would you like to drift to?
            </div>
            {destMode === 'chips' ? (
              <div style={{ display: 'flex', gap: 9, flexWrap: 'wrap' }}>
                {PLACES.map((p) => (
                  <button
                    key={p}
                    className={place === p ? 'chip selected' : 'chip'}
                    onClick={() => setPlace(p)}
                  >
                    {p}
                  </button>
                ))}
                <button className="chip-dashed" onClick={() => setDestMode('text')}>
                  In my own words…
                </button>
              </div>
            ) : (
              <>
                <input
                  className="text-input"
                  value={destText}
                  onChange={(e) => setDestText(e.target.value)}
                  placeholder="a quiet shoreline…"
                  maxLength={100}
                />
                <button
                  onClick={() => setDestMode('chips')}
                  style={{
                    alignSelf: 'flex-start',
                    background: 'none',
                    border: 'none',
                    padding: 0,
                    font: '400 11.5px var(--font-mono)',
                    color: 'var(--accent-text)',
                    cursor: 'pointer',
                  }}
                >
                  use the chips instead
                </button>
              </>
            )}
          </div>
        </div>

        <div
          style={{
            flex: 'none',
            padding: '16px 30px 34px',
            display: 'flex',
            flexDirection: 'column',
            gap: 14,
            background: 'var(--fade-bottom)',
          }}
        >
          <div style={{ display: 'flex', gap: 9 }}>
            {DURATIONS.map((n) => (
              <button
                key={n}
                className={n === duration ? 'seg selected' : 'seg'}
                onClick={() => setDuration(n)}
              >
                {n} min
              </button>
            ))}
          </div>
          <button
            className="btn-primary"
            onClick={() => void begin()}
            disabled={!moodOk || busy}
          >
            {busy ? 'Starting…' : 'Begin drifting'}
          </button>
          <div
            style={{
              minHeight: 17,
              textAlign: 'center',
              font: '400 12.5px var(--font-sans)',
              color: error ? 'var(--error)' : 'var(--text-hint)',
              transition: 'opacity .3s ease',
              opacity: error || !moodOk ? 1 : 0,
            }}
          >
            {error ?? "Pick how you're feeling to begin."}
          </div>
        </div>
      </div>
    </div>
  )
}
