/**
 * Shared knobs for the particle-cloud background. Most of what the cloud does
 * is derived from the route (see SceneLayer); the things only a page knows
 * are pushed through this context:
 *
 *   focus     the home sentence squashes the cloud while a button is held
 *   playing   the player pulses the cloud while audio runs, calms it paused
 *   cloudSrc  a user picture (object URL) sampled into the cloud — set by the
 *             home picture flow, kept through generating/player, cleared when
 *             the user lands back home
 *   dissolve  0..1 while the freshly chosen picture scatters into stardust
 *   heroDim   overrides the route's cloud opacity (the picture chooser rests
 *             the cloud at 0.34, per the prototype)
 *
 * The default value is a no-op so pages render in tests (and Storybook-style
 * isolation) without a provider.
 */
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useMemo,
  useState,
} from 'react'
import type { CloudFocus } from './ParticleCloud'

interface SceneControls {
  focus: CloudFocus
  setFocus: (focus: CloudFocus) => void
  /** null = not on the player screen. */
  playing: boolean | null
  setPlaying: (playing: boolean | null) => void
  /** '' = the procedural nebula. */
  cloudSrc: string
  setCloudSrc: (src: string) => void
  dissolve: number
  setDissolve: (dissolve: number) => void
  /** Back to the procedural nebula, fully dissolved; a blob URL is revoked. */
  resetCloud: () => void
  heroDim: number | null
  setHeroDim: (dim: number | null) => void
}

const SceneContext = createContext<SceneControls>({
  focus: 'idle',
  setFocus: () => {},
  playing: null,
  setPlaying: () => {},
  cloudSrc: '',
  setCloudSrc: () => {},
  dissolve: 1,
  setDissolve: () => {},
  resetCloud: () => {},
  heroDim: null,
  setHeroDim: () => {},
})

export function SceneProvider({ children }: { children: ReactNode }) {
  const [focus, setFocus] = useState<CloudFocus>('idle')
  const [playing, setPlaying] = useState<boolean | null>(null)
  const [cloudSrc, setCloudSrc] = useState('')
  const [dissolve, setDissolve] = useState(1)
  const [heroDim, setHeroDim] = useState<number | null>(null)
  const resetCloud = useCallback(() => {
    setCloudSrc((current) => {
      if (current.startsWith('blob:')) URL.revokeObjectURL(current)
      return ''
    })
    setDissolve(1)
  }, [])
  const value = useMemo(
    () => ({
      focus,
      setFocus,
      playing,
      setPlaying,
      cloudSrc,
      setCloudSrc,
      dissolve,
      setDissolve,
      resetCloud,
      heroDim,
      setHeroDim,
    }),
    [focus, playing, cloudSrc, dissolve, heroDim, resetCloud],
  )
  return <SceneContext.Provider value={value}>{children}</SceneContext.Provider>
}

export function useScene(): SceneControls {
  return useContext(SceneContext)
}
