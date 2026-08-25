import { describe, expect, it } from 'vitest'
import { dreamThumb } from './thumb'

describe('dreamThumb', () => {
  it('is deterministic per job id', () => {
    expect(dreamThumb('job-a')).toBe(dreamThumb('job-a'))
  })

  it('differs between jobs', () => {
    expect(dreamThumb('job-a')).not.toBe(dreamThumb('job-b'))
  })

  it('renders the prototype dot field: 34 gradients in a known tint band', () => {
    const css = dreamThumb('4f3c2a1e-aaaa-bbbb-cccc-121212121212')
    expect(css.match(/radial-gradient/g)).toHaveLength(34)
    expect(css).toMatch(/oklch\(0\.8\d 0\.0\d\d \d+ \/ 0\.\d\d\)/)
  })
})
