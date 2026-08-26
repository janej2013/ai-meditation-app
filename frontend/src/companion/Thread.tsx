/**
 * The conversation so far. Not chat bubbles: the listener's words sit right,
 * on a quiet chip; the companion's sit left with no ground at all, set like
 * a page of text. The crisis reply is the same text set apart by a rule
 * (design: [Companion · crisis reply]) -- no colour, no icon.
 */
import { CRISIS_PHONES } from './detectCrisis'
import type { Message } from './useCompanion'

function withPhoneLinks(text: string) {
  const parts: (string | { shown: string; tel: string })[] = []
  let rest = text
  for (;;) {
    let first: { index: number; phone: (typeof CRISIS_PHONES)[number] } | null = null
    for (const phone of CRISIS_PHONES) {
      const index = rest.indexOf(phone.shown)
      if (index !== -1 && (first === null || index < first.index)) first = { index, phone }
    }
    if (!first) break
    parts.push(rest.slice(0, first.index), first.phone)
    rest = rest.slice(first.index + first.phone.shown.length)
  }
  parts.push(rest)
  return parts.map((part, i) =>
    typeof part === 'string' ? (
      part
    ) : (
      <a key={i} className="crisis-phone" href={`tel:${part.tel}`}>
        {part.shown}
      </a>
    ),
  )
}

export default function Thread({ messages }: { messages: Message[] }) {
  return (
    <>
      {messages.map((m, i) =>
        m.role === 'user' ? (
          <div key={i} className="msg-row msg-row-user">
            <div className="msg-user">{m.text}</div>
          </div>
        ) : m.crisis ? (
          <div key={i} className="crisis-block">
            {withPhoneLinks(m.text)}
          </div>
        ) : (
          <div key={i} className="msg-companion">
            {m.text}
          </div>
        ),
      )}
    </>
  )
}
