/**
 * The shared mixer's ambient-to-session handoff.
 *
 * jsdom has no Web Audio, so a small fake stands in: enough of AudioContext
 * to count sources and observe start/stop. What is under test is the contract
 * the pages rely on -- the track that began on the home screen is the one the
 * player keeps, with no second copy layered on top and no refetch.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DualTrackMixer } from './mixer'

class FakeSource {
  buffer: unknown = null
  loop = false
  onended: (() => void) | null = null
  started = 0
  stopped = 0
  connect = vi.fn()
  disconnect = vi.fn()
  start = () => {
    this.started += 1
  }
  stop = () => {
    this.stopped += 1
  }
}

class FakeContext {
  state = 'suspended'
  currentTime = 0
  destination = {}
  sources: FakeSource[] = []
  resume = vi.fn(async () => {
    this.state = 'running'
  })
  suspend = vi.fn(async () => {
    this.state = 'suspended'
  })
  close = vi.fn(async () => {})
  createGain = () => ({
    gain: { value: 0, linearRampToValueAtTime: vi.fn() },
    connect: vi.fn(),
  })
  createBufferSource = () => {
    const s = new FakeSource()
    this.sources.push(s)
    return s
  }
  decodeAudioData = vi.fn(async (bytes: ArrayBuffer) => ({ duration: bytes.byteLength }))
}

let ctx: FakeContext
const fetchMock = vi.fn(async (url: string) => ({
  ok: true,
  arrayBuffer: async () => new ArrayBuffer(url.length),
}))

function stubAudioContext(): void {
  // Must be constructible ("new AudioContext()"), so not an arrow mock.
  vi.stubGlobal('AudioContext', function AudioContextStub() {
    return ctx
  } as unknown as typeof AudioContext)
}

beforeEach(() => {
  ctx = new FakeContext()
  stubAudioContext()
  vi.stubGlobal('fetch', fetchMock)
  fetchMock.mockClear()
})

afterEach(() => vi.unstubAllGlobals())

const running = () => ctx.sources.filter((s) => s.started > s.stopped)

describe('ambient background music', () => {
  it('starts inside the gesture and resumes the context', async () => {
    const mixer = new DualTrackMixer()
    await mixer.startAmbient('https://cdn/assets/bgm/default_bgm.mp3')

    expect(ctx.resume).toHaveBeenCalled()
    expect(running()).toHaveLength(1)
    expect(running()[0].loop).toBe(true)
  })

  it('is kept, not restarted or refetched, when the narration joins', async () => {
    const mixer = new DualTrackMixer()
    await mixer.startAmbient('https://cdn/assets/bgm/default_bgm.mp3')
    const ambient = running()[0]

    await mixer.loadNarration('https://cdn/jobs/j/narration.mp3?sig')
    await mixer.loadBgm('https://cdn/assets/bgm/default_bgm.mp3') // what the player does on mount
    await mixer.play()

    expect(fetchMock).toHaveBeenCalledTimes(2) // one BGM, one narration
    expect(ambient.stopped).toBe(0)
    expect(running()).toHaveLength(2) // narration + the original BGM source
    expect(mixer.isPlaying()).toBe(true)
  })

  it('switching tracks mid-ambient replaces only the music', async () => {
    const mixer = new DualTrackMixer()
    await mixer.startAmbient('https://cdn/assets/bgm/default_bgm.mp3')
    const first = running()[0]

    await mixer.loadBgm('https://cdn/assets/bgm/other.mp3')

    expect(first.stopped).toBe(1)
    expect(running()).toHaveLength(1)
    expect(running()[0]).not.toBe(first)
  })

  it('endSession stops everything, resets playing, and keeps the context reusable', async () => {
    const mixer = new DualTrackMixer()
    await mixer.startAmbient('https://cdn/assets/bgm/default_bgm.mp3')
    await mixer.loadNarration('https://cdn/jobs/j/narration.mp3?sig')
    await mixer.play()

    mixer.endSession()

    expect(running()).toHaveLength(0)
    expect(mixer.isPlaying()).toBe(false)
    expect(ctx.suspend).toHaveBeenCalled()
    expect(ctx.close).not.toHaveBeenCalled()

    // The next session's Begin reuses the same context and cached buffer.
    await mixer.startAmbient('https://cdn/assets/bgm/default_bgm.mp3')
    expect(running()).toHaveLength(1)
  })

  it('stopAmbient silences the music and leaves the context for the player', async () => {
    const mixer = new DualTrackMixer()
    await mixer.startAmbient('https://cdn/assets/bgm/default_bgm.mp3')

    mixer.stopAmbient()

    expect(running()).toHaveLength(0)
    expect(ctx.close).not.toHaveBeenCalled()
  })

  it('a stopAmbient that lands while the track is still loading wins', async () => {
    // The race behind a refused start: Begin/Start began the fetch, the
    // server said no a moment later, and the decode has not landed yet.
    let release!: () => void
    fetchMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = () => resolve({ ok: true, arrayBuffer: async () => new ArrayBuffer(8) })
        }),
    )
    const mixer = new DualTrackMixer()
    const pending = mixer.startAmbient('https://cdn/assets/bgm/default_bgm.mp3')
    // The context resumes first; the fetch is in flight once fetch was called.
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))

    mixer.stopAmbient()
    release()
    await pending

    expect(running()).toHaveLength(0)
    // The decoded track is kept: the next Begin starts it without a refetch.
    await mixer.startAmbient('https://cdn/assets/bgm/default_bgm.mp3')
    expect(running()).toHaveLength(1)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('never throws without Web Audio or when the fetch fails', async () => {
    vi.stubGlobal('AudioContext', undefined)
    await expect(
      new DualTrackMixer().startAmbient('https://cdn/x.mp3'),
    ).resolves.toBeUndefined()

    stubAudioContext()
    fetchMock.mockResolvedValueOnce({ ok: false, arrayBuffer: async () => new ArrayBuffer(0) })
    await expect(
      new DualTrackMixer().startAmbient('https://cdn/x.mp3'),
    ).resolves.toBeUndefined()
    expect(running()).toHaveLength(0)
  })
})
