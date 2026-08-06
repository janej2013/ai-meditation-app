/**
 * Can a headless browser actually decode our audio?
 *
 * The whole player rests on `fetch` + `decodeAudioData`, and if headless
 * Chromium cannot do that, every player test has to fall back to asserting
 * that the mixer was *called* rather than that it worked -- a much weaker
 * check. This settles it before any of those tests are written, and keeps
 * settling it: a browser or CI image that loses audio support fails here with
 * an obvious reason rather than as a mystery elsewhere.
 *
 * The fixture is the BGM asset the pipeline already ships, so this exercises a
 * real MP3 rather than something invented for the test.
 */
import { expect, test } from '@playwright/test'
import path from 'node:path'

const MP3 = path.resolve(import.meta.dirname, '../../assets/bgm/silence.mp3')

test('headless Chromium decodes a real MP3 into an AudioBuffer', async ({ page }) => {
  await page.route('**/fixture.mp3', (route) =>
    route.fulfill({
      path: MP3,
      contentType: 'audio/mpeg',
      // Mimics the header the audio distribution sends. Same-origin here, so
      // CORS is not actually exercised -- proving the *cross-origin* fetch
      // works against the real response-headers policy is smoke-test
      // territory, where the site and audio origins genuinely differ.
      headers: { 'access-control-allow-origin': '*' },
    }),
  )

  await page.goto('/')

  const decoded = await page.evaluate(async () => {
    const response = await fetch('/fixture.mp3', { mode: 'cors' })
    const bytes = await response.arrayBuffer()
    const ctx = new AudioContext()
    const buffer = await ctx.decodeAudioData(bytes)
    return { duration: buffer.duration, channels: buffer.numberOfChannels }
  })

  expect(decoded.duration).toBeGreaterThan(0)
  expect(decoded.channels).toBeGreaterThan(0)
})

test('an AudioContext starts without an output device', async ({ page }) => {
  await page.goto('/')

  // Headless has no sound card. Chromium falls back to a null sink, so a
  // context still reaches "running" and the mixer's clock -- which drives
  // elapsed(), the progress ring and onEnded -- advances.
  const state = await page.evaluate(async () => {
    const ctx = new AudioContext()
    await ctx.resume()
    const before = ctx.currentTime
    await new Promise((r) => setTimeout(r, 250))
    return { state: ctx.state, advanced: ctx.currentTime > before }
  })

  expect(state.state).toBe('running')
  expect(state.advanced).toBe(true)
})
