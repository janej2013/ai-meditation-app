import { describe, expect, it } from 'vitest'
import { wovenAgo } from './wovenAgo'

const NOW = new Date('2026-08-24T12:00:00Z')
const daysAgo = (n: number) => new Date(NOW.getTime() - n * 86_400_000)

describe('wovenAgo', () => {
  it.each([
    [daysAgo(0), 'woven today'],
    [daysAgo(1), 'woven last night'],
    [daysAgo(3), 'woven 3 nights ago'],
    [daysAgo(6), 'woven 6 nights ago'],
    [daysAgo(7), 'woven last week'],
    [daysAgo(13), 'woven last week'],
    [daysAgo(14), 'woven two weeks ago'],
    [daysAgo(35), 'woven five weeks ago'],
    [daysAgo(80), 'woven in June'],
    [new Date('2025-12-30T12:00:00Z'), 'woven in December 2025'],
  ])('%s -> %s', (created, expected) => {
    expect(wovenAgo(created, NOW)).toBe(expected)
  })

  it('never says "0 nights ago" for a future clock skew', () => {
    expect(wovenAgo(new Date(NOW.getTime() + 60_000), NOW)).toBe('woven today')
  })
})
