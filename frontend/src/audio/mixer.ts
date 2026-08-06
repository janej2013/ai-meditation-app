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
    this.stopSources()
    this.narrationBuffer = await this.fetchBuffer(url)
    this.offset = 0
    this.playing = false
  }

  /**
   * Load (or switch) the background track — callable mid-session. Playback
   * of the narration is never interrupted: only the BGM source is replaced.
   */
  async loadBgm(url: string | null): Promise<void> {
    if (this.bgmSource) {
      this.bgmSource.stop()
      this.bgmSource.disconnect()
      this.bgmSource = null
    }
    this.bgmBuffer = url ? await this.fetchBuffer(url) : null
    if (this.playing && this.bgmBuffer) this.startBgm()
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
    if (!this.bgmBuffer || !this.bgmGain) return
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

  private stopSources(): void {
    for (const source of [this.narrationSource, this.bgmSource]) {
      if (source) {
        try {
          source.stop()
        } catch {
          // Already stopped — harmless.
        }
        source.disconnect()
      }
    }
    this.narrationSource = null
    this.bgmSource = null
  }

  dispose(): void {
    this.stopSources()
    this.narrationBuffer = null
    this.bgmBuffer = null
    void this.ctx?.close()
    this.ctx = null
  }
}

/** The built-in BGM tracks under assets/ on the audio distribution. */
export interface BgmTrack {
  id: string
  label: string
  path: string | null // null = narration only
}

export const BGM_TRACKS: BgmTrack[] = [
  { id: 'silence', label: 'Soft pad', path: 'assets/bgm/silence.mp3' },
  { id: 'none', label: 'Voice only', path: null },
]

export function bgmUrl(track: BgmTrack): string | null {
  const domain: string = import.meta.env.VITE_AUDIO_DOMAIN ?? ''
  if (!track.path || !domain) return null
  return `https://${domain}/${track.path}`
}
