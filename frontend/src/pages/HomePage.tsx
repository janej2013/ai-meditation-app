/**
 * Home, straight from the prototype: a bare "Drift from words · or · a
 * picture" sentence over the particle cloud, opening into either the panel —
 * how are you feeling (chips or own words), where to drift to, duration,
 * Begin drifting — or the picture chooser.
 *
 * The picture path is available to everyone. The chosen file is normalised to
 * a small JPEG in the browser (picture/prepare.ts); that blob becomes the
 * object URL the particle cloud samples into the dreamscape (ParticleCloud's
 * src prop), and is uploaded to S3 in the background while the user fills in
 * the panel. Begin waits for the upload and hands its id to POST /generate,
 * where the pipeline's describe_picture step turns it into the keywords the
 * waiting screen shows ("In your picture, we found…").
 *
 * The API takes one mood string; the destination is folded into it, so the
 * backend contract is unchanged.
 */
import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError, NotSignedInError, startGeneration, uploadPicture } from '../api/client'
import { DEFAULT_BGM_TRACK, bgmUrl, mixer } from '../audio/mixer'
import { isSignedIn } from '../auth/cognito'
import { useDreamCount } from '../dreamscapes/useDreamscapes'
import { prepareJpeg } from '../picture/prepare'
import { useScene } from '../scene/SceneContext'

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
  const { setFocus, cloudSrc, setCloudSrc, setDissolve, setHeroDim } = useScene()

  const [view, setView] = useState<'sentence' | 'picture' | 'panel'>('sentence')
  const [moods, setMoods] = useState<string[]>([])
  const [moodMode, setMoodMode] = useState<'chips' | 'text'>('chips')
  const [moodText, setMoodText] = useState('')
  const [place, setPlace] = useState('Anywhere')
  const [destMode, setDestMode] = useState<'chips' | 'text'>('chips')
  const [destText, setDestText] = useState('')
  const [duration, setDuration] = useState(10)

  const [signedIn, setSignedIn] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const dreamCount = useDreamCount()
  const fileRef = useRef<HTMLInputElement>(null)
  const dissolveTimer = useRef<ReturnType<typeof setInterval> | null>(null)
  // The in-flight upload of the current picture, started the moment it is
  // chosen so it is usually done by the time Begin is pressed. Null when the
  // session is words-only.
  const upload = useRef<Promise<string> | null>(null)

  useEffect(() => {
    // Only whether to gate Begin behind sign-in; the balance itself lives in
    // the shell's AccountPill, which every screen shares.
    void (async () => {
      if (await isSignedIn()) setSignedIn(true)
    })()
  }, [])

  // Landing home starts a fresh session: release the previous picture (it was
  // only ever an object URL in this browser) and settle the cloud.
  useEffect(() => {
    if (cloudSrc) URL.revokeObjectURL(cloudSrc)
    setCloudSrc('')
    setDissolve(1)
    return () => {
      if (dissolveTimer.current) clearInterval(dissolveTimer.current)
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

  const onFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files && e.target.files[0]
    // Clear the input now: browsers fire no change event when the same file
    // is re-picked, which would dead-end the "please choose it again" path.
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
    if (cloudSrc) URL.revokeObjectURL(cloudSrc)
    setCloudSrc(URL.createObjectURL(picture))
    // Not awaited: the user fills in the panel meanwhile. A rejection is
    // caught at Begin, where there is something useful to say about it.
    const pending = uploadPicture(picture)
    pending.catch(() => undefined)
    upload.current = pending
    // Crisp picture first, then scattering into the dreamy cloud.
    setDissolve(0)
    if (dissolveTimer.current) clearInterval(dissolveTimer.current)
    const t0 = Date.now()
    dissolveTimer.current = setInterval(() => {
      const p = Math.min(1, (Date.now() - t0) / DISSOLVE_MS)
      setDissolve(p)
      if (p >= 1 && dissolveTimer.current) clearInterval(dissolveTimer.current)
    }, 60)
    setView('panel')
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
    setBusy(true)
    setError(null)
    if (dissolveTimer.current) clearInterval(dissolveTimer.current)
    setDissolve(1)
    // Still inside the click: the only place a mobile browser lets audio
    // start. The music then runs through the waiting screen into the player.
    void mixer.startAmbient(bgmUrl(DEFAULT_BGM_TRACK))
    try {
      let pictureId: string | undefined
      if (upload.current) {
        try {
          pictureId = await upload.current
        } catch (e) {
          if (e instanceof NotSignedInError) throw e
          mixer.stopAmbient()
          setError("Your picture didn't finish uploading. Please choose it again.")
          return
        }
      }
      const { job_id } = await startGeneration(mood, duration, pictureId)
      navigate(`/generating/${job_id}`, {
        state: { duration, feeling, destination, pic: cloudSrc !== '' },
      })
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
              // Taking the words path drops any picture chosen earlier: the
              // upload id must not ride along into a words-only session.
              if (cloudSrc) URL.revokeObjectURL(cloudSrc)
              setCloudSrc('')
              upload.current = null
              setDissolve(1)
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
        {/* The collection's entry line: absent (not a spinner) until the
            count is known, then fades in with it. */}
        <div
          style={{
            marginTop: 44,
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
          <button className="btn-primary" onClick={() => fileRef.current?.click()}>
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

      {/* Panel view — the actual generation form. */}
      <div
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
          {cloudSrc !== '' && (
            <button className="btn-ghost" onClick={() => setView('picture')}>
              Choose another picture
            </button>
          )}
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
