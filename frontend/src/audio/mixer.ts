/**
 * Two-track playback: narration + background music, mixed in the browser
 * with the Web Audio API (CLAUDE.md: the pipeline delivers narration only,
 * and mixing client-side is what lets the listener switch BGM or change its
 * volume mid-session).
 *
 * Topology:
 *
 *   narration (signed URL)  ──► GainNode (1.0) ──┐
 *                                                ├──► destination
 *   bgm (assets/*, looped)  ──► GainNode (0.2) ──┘
 *
 * Both sources are fetched with CORS (`fetch` + decodeAudioData): the audio
 * CloudFront distribution sends CORS headers on both behaviours, and without
 * them the decode fails — a same-origin <audio> tag would play, but Web Audio
 * cannot touch cross-origin data it cannot verify.
 *
 * The narration drives the clock: elapsed/duration/onEnded all follow it, and
 * the BGM simply loops underneath for as long as the narration runs.
 *
 * One mixer is shared by the whole app (`mixer` below) so the background
 * track can start on the home screen -- inside the click that begins a
 * session, which is what mobile autoplay rules require -- keep playing through
 * the waiting screen, and carry on unbroken when the narration joins it in
 * the player.
 */

export interface MixerState {
  playing: boolean
  elapsed: number
  duration: number
}

export const DEFAULT_BGM_VOLUME = 0.2

export class DualTrackMixer {
  private ctx: AudioContext | null = null
  private narrationBuffer: AudioBuffer | null = null
  private bgmBuffer: AudioBuffer | null = null

  private narrationSource: AudioBufferSourceNode | null = null
  private bgmSource: AudioBufferSourceNode | null = null
  private bgmUrl: string | null = null // what bgmBuffer was decoded from
  private narrationGain: GainNode | null = null
  private bgmGain: GainNode | null = null

  private startedAt = 0 // ctx.currentTime when playback (re)started
  private offset = 0 // seconds into the narration when paused
  private bgmVolume = DEFAULT_BGM_VOLUME
  private playing = false

  onEnded: (() => void) | null = null

  private ensureContext(): AudioContext {
    if (!this.ctx) {
      this.ctx = new AudioContext()
      this.narrationGain = this.ctx.createGain()
      this.narrationGain.gain.value = 1
      this.narrationGain.connect(this.ctx.destination)
      this.bgmGain = this.ctx.createGain()
      this.bgmGain.gain.value = this.bgmVolume
      this.bgmGain.connect(this.ctx.destination)
    }
    return this.ctx
  }

  private async fetchBuffer(url: string): Promise<AudioBuffer> {
    // cors mode is what crossOrigin="anonymous" means for fetch: no
    // credentials, and the response must carry Access-Control-Allow-Origin.
    const response = await fetch(url, { mode: 'cors' })
    if (!response.ok) throw new Error(`audio fetch failed: ${response.status}`)
    const bytes = await response.arrayBuffer()
    return this.ensureContext().decodeAudioData(bytes)
  }

  /** Load the narration (signed URL). Resets position to the start. */
  async loadNarration(url: string): Promise<void> {
    this.stopNarration()
    this.narrationBuffer = await this.fetchBuffer(url)
    this.offset = 0
    this.playing = false
  }

  /**
   * Load (or switch) the background track — callable mid-session. Playback
   * of the narration is never interrupted: only the BGM source is replaced.
   * A track that is already decoded is not fetched again, so arriving in the
   * player with the ambient track running costs nothing.
   */
  async loadBgm(url: string | null): Promise<void> {
    if (url && url === this.bgmUrl && this.bgmBuffer) return
    const wasRunning = this.bgmSource !== null
    this.stopBgm()
    this.bgmBuffer = url ? await this.fetchBuffer(url) : null
    this.bgmUrl = url
    if ((this.playing || wasRunning) && this.bgmBuffer) this.startBgm()
  }

  /**
   * Start the background track on its own, before there is a narration.
   * Must be called from a user gesture: the context is created and resumed
   * synchronously here, and the fetch that follows is allowed to outlive the
   * gesture. Never throws -- a session without music is still a session.
   */
  async startAmbient(url: string | null): Promise<void> {
    if (!url) return
    try {
      const ctx = this.ensureContext()
      if (ctx.state === 'suspended') await ctx.resume()
      await this.loadBgm(url)
      if (!this.bgmSource) this.startBgm()
    } catch {
      // No AudioContext (tests), a blocked fetch, a decode failure: silence.
    }
  }

  /** Stop the background track without touching the narration. */
  stopAmbient(): void {
    this.stopBgm()
  }

  /**
   * The player is done: stop narration and music, forget the narration, and
   * suspend the context so the browser releases audio focus (iOS keeps the
   * "playing audio" state alive for a running context). Decoded BGM and the
   * context itself survive -- the next session's Begin resumes and reuses
   * them without a refetch.
   */
  endSession(): void {
    this.stopSources()
    this.narrationBuffer = null
    this.offset = 0
    this.playing = false
    void this.ctx?.suspend()
  }

  setBgmVolume(volume: number): void {
    this.bgmVolume = Math.max(0, Math.min(1, volume))
    if (this.bgmGain && this.ctx) {
      // A short ramp instead of a click.
      this.bgmGain.gain.linearRampToValueAtTime(this.bgmVolume, this.ctx.currentTime + 0.08)
    }
  }

  getBgmVolume(): number {
    return this.bgmVolume
  }

  private startBgm(): void {
    const ctx = this.ensureContext()
    if (!this.bgmBuffer || !this.bgmGain || this.bgmSource) return
    this.bgmSource = ctx.createBufferSource()
    this.bgmSource.buffer = this.bgmBuffer
    this.bgmSource.loop = true
    this.bgmSource.connect(this.bgmGain)
    this.bgmSource.start()
  }

  async play(): Promise<void> {
    const ctx = this.ensureContext()
    if (!this.narrationBuffer || this.playing) return
    // Browsers suspend fresh contexts until a user gesture; play() is always
    // called from one, so resume here.
    if (ctx.state === 'suspended') await ctx.resume()

    this.narrationSource = ctx.createBufferSource()
    this.narrationSource.buffer = this.narrationBuffer
    this.narrationSource.connect(this.narrationGain!)
    this.narrationSource.onended = () => {
      // Fires for both natural end and stop(); only report the former.
      if (this.playing && this.elapsed() >= this.duration() - 0.25) {
        this.pause()
        this.offset = 0
        this.onEnded?.()
      }
    }
    this.narrationSource.start(0, this.offset)
    this.startedAt = ctx.currentTime
    this.playing = true
    this.startBgm()
  }

  pause(): void {
    if (!this.playing) return
    this.offset = this.elapsed()
    this.playing = false
    this.stopSources()
  }

  /** Jump to a position (seconds). Keeps the current play/pause state. */
  async seek(seconds: number): Promise<void> {
    const wasPlaying = this.playing
    if (this.playing) this.pause()
    this.offset = Math.max(0, Math.min(seconds, this.duration()))
    if (wasPlaying) await this.play()
  }

  elapsed(): number {
    if (!this.ctx) return this.offset
    return this.playing
      ? Math.min(this.offset + (this.ctx.currentTime - this.startedAt), this.duration())
      : this.offset
  }

  duration(): number {
    return this.narrationBuffer?.duration ?? 0
  }

  isPlaying(): boolean {
    return this.playing
  }

  state(): MixerState {
    return { playing: this.playing, elapsed: this.elapsed(), duration: this.duration() }
  }

  private stopNarration(): void {
    stopSource(this.narrationSource)
    this.narrationSource = null
  }

  private stopBgm(): void {
    stopSource(this.bgmSource)
    this.bgmSource = null
  }

  private stopSources(): void {
    this.stopNarration()
    this.stopBgm()
  }
}

function stopSource(source: AudioBufferSourceNode | null): void {
  if (!source) return
  try {
    source.stop()
  } catch {
    // Already stopped — harmless.
  }
  source.disconnect()
}

/** The app-wide mixer. Lazy: no AudioContext exists until something plays. */
export const mixer = new DualTrackMixer()

/** The built-in BGM tracks under assets/ on the audio distribution. */
export interface BgmTrack {
  id: string
  label: string
  path: string | null // null = narration only
}

// The first entry is the default: it starts on the home screen when a session
// begins and runs through the waiting screen into the player. The tracks are
// licensed from Pixabay and uploaded to the bucket by hand (`make upload-bgm`),
// never committed -- see assets/bgm/README.md. silence.mp3 also lives under
// assets/ but is a CI probe, not something to offer a listener.
export const BGM_TRACKS: BgmTrack[] = [
  { id: 'default', label: 'Meditation', path: 'assets/bgm/default_bgm.mp3' },
  { id: 'none', label: 'Voice only', path: null },
]

export const DEFAULT_BGM_TRACK: BgmTrack = BGM_TRACKS[0]

export function bgmUrl(track: BgmTrack): string | null {
  const domain: string = import.meta.env.VITE_AUDIO_DOMAIN ?? ''
  if (!track.path || !domain) return null
  return `https://${domain}/${track.path}`
}
