/**
 * What a dreamscape is called — one formatter for the collection card and
 * the player's revisit header, so the two never disagree.
 *
 * Picture jobs carry keywords; the prototype titles them "Dusk · Ocean · Longing"
 * on a card and splits them "Dusk · Ocean ·" / "Longing" in the player header.
 * Text jobs carry the mood excerpt instead.
 */

const FALLBACK = 'A quiet session'

export function dreamTitle(
  keywords: string[] | null | undefined,
  moodExcerpt?: string | null,
): string {
  return keywords?.length ? keywords.join(' · ') : (moodExcerpt ?? FALLBACK)
}

/** The player's two-line form; a single line when there is nothing to split. */
export function dreamTitleLines(
  keywords: string[] | null | undefined,
  moodExcerpt?: string | null,
): string[] {
  if (keywords && keywords.length >= 3) {
    return [`${keywords[0]} · ${keywords[1]} ·`, keywords.slice(2).join(' · ')]
  }
  return [dreamTitle(keywords, moodExcerpt)]
}
