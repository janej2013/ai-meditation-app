/**
 * "woven last night", "woven 3 nights ago" — the collection's relative time,
 * phrased the way the prototype phrases it. A pure function of two dates so
 * tests can pin every tier without faking clocks.
 */

const SMALL = ['', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine']

const MONTHS = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December',
]

export function wovenAgo(created: Date, now: Date = new Date()): string {
  const days = Math.floor((now.getTime() - created.getTime()) / 86_400_000)
  if (days <= 0) return 'woven today'
  if (days === 1) return 'woven last night'
  if (days < 7) return `woven ${days} nights ago`
  if (days < 14) return 'woven last week'
  if (days < 60) {
    const weeks = Math.floor(days / 7)
    return `woven ${SMALL[weeks] ?? weeks} weeks ago`
  }
  const month = MONTHS[created.getMonth()]
  return created.getFullYear() === now.getFullYear()
    ? `woven in ${month}`
    : `woven in ${month} ${created.getFullYear()}`
}
