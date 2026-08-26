/**
 * Whether a companion reply is the crisis response.
 *
 * The runner sends it as ordinary text: the model is instructed to answer a
 * crisis signal with one fixed paragraph (`backend/agent/prompt.py`,
 * CRISIS_TEXT), and nothing in the event stream marks it. The Lifeline number
 * is the anchor both sides share -- it appears in no other reply -- so it is
 * what the PWA keys the calmer, set-apart styling on. Change one side, change
 * the other.
 */
export const CRISIS_ANCHOR = '13 11 14'

export function isCrisisReply(text: string): boolean {
  return text.includes(CRISIS_ANCHOR)
}

/** The numbers in the crisis text, each made tappable. */
export const CRISIS_PHONES: ReadonlyArray<{ shown: string; tel: string }> = [
  { shown: '13 11 14', tel: '131114' },
  { shown: '1300 22 4636', tel: '1300224636' },
  { shown: '000', tel: '000' },
]
