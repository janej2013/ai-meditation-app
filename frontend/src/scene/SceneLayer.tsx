/**
 * The always-present background: particle cloud + scrim, sitting behind every
 * route. Opacity, mood, pulse and scrim strength per screen are lifted from
 * the prototype's renderVals mapping (Meditation PWA Prototype.dc.html).
 */
import { Suspense, lazy, useEffect } from 'react'
import { useLocation } from 'react-router-dom'
import type { CloudMood } from './ParticleCloud'
import { useScene } from './SceneContext'

// three.js is by far the heaviest dependency; keep it out of the entry chunk
// so the app is interactive before the cloud fades in.
const ParticleCloud = lazy(() => import('./ParticleCloud'))

interface RouteScene {
  heroOpacity: number
  mood: CloudMood
  scrimOpacity: number
  paused: boolean
}

function sceneFor(pathname: string, withPicture: boolean): RouteScene {
  if (pathname === '/') return { heroOpacity: 1, mood: 'hero', scrimOpacity: 1, paused: false }
  if (pathname.startsWith('/generating'))
    // Words mode paints its own radial wash and rests the cloud; a picture
    // session keeps its dreamscape front and centre (the prototype's picMode).
    return withPicture
      ? { heroOpacity: 1, mood: 'hero', scrimOpacity: 0, paused: false }
      : { heroOpacity: 0, mood: 'hero', scrimOpacity: 1, paused: true }
  if (pathname.startsWith('/player'))
    return { heroOpacity: 0.85, mood: 'ambient', scrimOpacity: 0, paused: false }
  if (pathname === '/failed' || pathname === '/plans')
    return { heroOpacity: 0.52, mood: 'settle', scrimOpacity: 0.5, paused: false }
  if (
    pathname === '/signup' ||
    pathname === '/verify' ||
    pathname === '/account' ||
    pathname === '/privacy'
  )
    return { heroOpacity: 0.56, mood: 'whisper', scrimOpacity: 0.5, paused: false }
  // The prototype rests the cloud at 0.50 behind a conversation, as it does
  // behind the collection: present, but not competing with the text.
  if (pathname === '/companion')
    return { heroOpacity: 0.5, mood: 'whisper', scrimOpacity: 0.5, paused: false }
  return { heroOpacity: 0.56, mood: 'whisper', scrimOpacity: 0.5, paused: false }
}

/** The routes a chosen picture travels through; anywhere else clears it. */
const CARRIES_PICTURE = /^\/(|generating\/.*|player\/.*)$/

export default function SceneLayer() {
  const { pathname } = useLocation()
  const { focus, playing, cloudSrc, dissolve, heroDim, resetCloud } = useScene()

  // One place decides where the user's picture may show: home (where it is
  // chosen), the waiting screen and the player. Leaving that flow -- to sign
  // in, the account, the plans, the collection -- drops it, so no other
  // screen ever sits on someone's photo and no page has to remember to reset.
  useEffect(() => {
    if (!CARRIES_PICTURE.test(pathname)) resetCloud()
  }, [pathname, resetCloud])
  const scene = sceneFor(pathname, cloudSrc !== '')

  const onPlayer = pathname.startsWith('/player')
  const pulse = onPlayer && playing === true
  const calm = onPlayer && playing === false

  return (
    <div aria-hidden style={{ position: 'absolute', inset: 0, zIndex: 0 }}>
      <div
        style={{
          position: 'absolute',
          inset: 0,
          transition: 'opacity 1.4s ease',
          opacity: heroDim ?? scene.heroOpacity,
        }}
      >
        <Suspense fallback={null}>
          <ParticleCloud
            paused={scene.paused}
            focus={focus}
            mood={scene.mood}
            pulse={pulse}
            calm={calm}
            src={cloudSrc || undefined}
            dissolve={dissolve}
          />
        </Suspense>
      </div>
      <div
        style={{
          position: 'absolute',
          inset: 0,
          pointerEvents: 'none',
          transition: 'opacity 1.4s ease',
          opacity: scene.scrimOpacity,
          background: 'var(--scrim)',
        }}
      />
    </div>
  )
}
