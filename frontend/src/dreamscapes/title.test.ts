import { describe, expect, it } from 'vitest'
import { dreamTitle, dreamTitleLines } from './title'

describe('dreamTitle', () => {
  it('joins keywords with middle dots', () => {
    expect(dreamTitle(['Dusk', 'Ocean', 'Longing'])).toBe('Dusk · Ocean · Longing')
  })
  it('falls back to the mood excerpt, then to a neutral name', () => {
    expect(dreamTitle(null, 'calm after rain')).toBe('calm after rain')
    expect(dreamTitle([], null)).toBe('A quiet session')
  })
})

describe('dreamTitleLines', () => {
  it('splits three-or-more keywords the way the prototype header does', () => {
    expect(dreamTitleLines(['Dusk', 'Ocean', 'Longing'])).toEqual(['Dusk · Ocean ·', 'Longing'])
    expect(dreamTitleLines(['A', 'B', 'C', 'D'])).toEqual(['A · B ·', 'C · D'])
  })
  it('keeps fewer keywords, or an excerpt, on one line', () => {
    expect(dreamTitleLines(['Dusk', 'Ocean'])).toEqual(['Dusk · Ocean'])
    expect(dreamTitleLines(null, 'calm')).toEqual(['calm'])
  })
})
