/**
 * The card thumbnail: a seeded field of soft dots, exactly the prototype's
 * `thumbField` — a CSS background-image of 34 radial gradients, not a WebGL
 * render. Deterministic per job id, so a card looks the same on every visit,
 * and free: no context, no canvas, no cache to manage.
 */

// The prototype's dream tints: L/C in the glow band, hues spread apart.
const TINTS = [
  '0.80 0.075 285',
  '0.82 0.070 205',
  '0.84 0.075 70',
  '0.80 0.055 265',
  '0.85 0.055 330',
]

/** FNV-1a, enough to spread uuid strings into distinct seeds. */
function hash(text: string): number {
  let h = 0x811c9dc5
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return h >>> 0
}

export function dreamThumb(jobId: string): string {
  const tint = TINTS[hash(jobId) % TINTS.length]
  // The prototype's LCG, seeded from the id instead of a hand-picked number.
  let s = hash(`seed:${jobId}`) % 2147483648
  const rnd = () => (s = (s * 1103515245 + 12345) % 2147483648) / 2147483648
  const dots: string[] = []
  for (let i = 0; i < 34; i++) {
    const x = (6 + rnd() * 88).toFixed(1)
    const y = (6 + rnd() * 88).toFixed(1)
    const a = (0.22 + rnd() * 0.62).toFixed(2)
    const r = (0.9 + rnd() * 1.5).toFixed(1)
    dots.push(
      `radial-gradient(circle at ${x}% ${y}%, oklch(${tint} / ${a}) 0, oklch(${tint} / 0) ${r}px)`,
    )
  }
  return dots.join(',')
}
