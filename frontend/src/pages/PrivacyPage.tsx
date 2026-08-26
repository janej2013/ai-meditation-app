/**
 * What Drift keeps, in the listener's words. The annotated source -- every
 * claim with the code that enforces it -- is docs/privacy.md; this page says
 * the same things with fewer of them, never more (a test holds the numbers
 * to the document). Readable signed out: it is for people deciding whether
 * to sign up.
 */
import { useLocation, useNavigate } from 'react-router-dom'

/** Where to write about an account. Empty until there is a channel; the
 *  sentence that needs it is not shown before then. */
export const PRIVACY_CONTACT = ''
/** The date this page was last read against docs/privacy.md and the code. */
export const LAST_CHECKED = '2026-08-26'

const h2 = {
  marginTop: 30,
  font: '500 15px/1.4 var(--font-sans)',
  color: 'var(--text-primary)',
} as const
const p = {
  marginTop: 8,
  font: '400 14px/1.6 var(--font-sans)',
  color: 'var(--text-body)',
} as const

const KEPT: [string, string, string][] = [
  ['Email and password', 'while you have an account', 'not yet self-service'],
  ['Words you typed for a meditation', 'while you have an account', 'let the dreamscape go'],
  ['Narration audio', '90 days', 'expires on its own'],
  ['A picture you uploaded', '365 days', 'expires on its own'],
  ['The companion\u2019s conversation', '30 days', 'expires on its own'],
  ['What the companion remembers', 'until you clear it', 'Account \u2192 Forget everything'],
  ['Payment records', 'ids only; Stripe keeps the rest', 'with Stripe'],
]

export default function PrivacyPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const from = (location.state as { from?: string } | null)?.from

  return (
    <div className="screen" style={{ background: 'var(--wash-plans)', overflowY: 'auto' }}>
      <div style={{ marginTop: 20, flex: 'none' }}>
        <button className="btn-back" onClick={() => (from ? navigate(-1) : navigate('/'))}>
          ← Back
        </button>
      </div>

      <h1
        style={{
          marginTop: 40,
          font: '400 27px/1.35 var(--font-sans)',
          color: 'var(--text-primary)',
        }}
      >
        What Drift keeps
      </h1>
      <div style={p}>
        Everything here runs in Sydney. The models that write and read for you are called as
        needed and your words and pictures stay in Australia. This page says what we actually do
        with what you give us, and for how long.
      </div>

      <h2 style={h2}>Your account</h2>
      <div style={p}>
        Your email address and password are held by the sign-in service; we never see the
        password. We keep your balance, your plan and, if you subscribe, the subscription
        reference. Card details go to Stripe on Stripe&apos;s own pages and never reach us; the
        payment records we keep are ids, and they expire after 30 days.
      </div>
      <div style={p}>
        There is no button to delete an account yet.
        {PRIVACY_CONTACT && ` Write to ${PRIVACY_CONTACT} and we will remove it by hand.`}
      </div>

      <h2 style={h2}>Meditations from words</h2>
      <div style={p}>
        What you type is kept with that meditation so you can come back to it and hear it again.
        The model that writes the script is told not to repeat personal details back to you. The
        narration is kept for 90 days; the words stay so it can be made again.
      </div>

      <h2 style={h2}>Meditations from a picture</h2>
      <div style={p}>
        A picture you upload is private to your account and kept for 365 days, so a revisit can
        show it again. The model looks at it once and keeps only a few mood words and a one-line
        summary; it is told not to describe people and not to read out any text in the picture.
        Those words are yours and are not written to logs.
      </div>

      <h2 style={h2}>The companion</h2>
      <div style={p}>
        A conversation is saved one reply at a time so it can pick up after a reload, and it
        expires after 30 days. What it remembers is a short list of things you told it about your
        meditations, kept until you clear it: Account, then What it remembers, then Forget
        everything. It can see a summary of your own earlier meditations, that list, and the
        conversation you are in. Nothing from anyone else.
      </div>
      <div style={p}>
        It cannot start a meditation or use a credit. It can only suggest one; the meditation
        starts when you tap Start, and that tap is the only thing that uses a credit. If a message
        reads as a crisis it answers with the same words for everyone, Lifeline on 13 11 14 and
        000 in an emergency, and suggests nothing. That is not medical advice. You can have 30
        conversations a month.
      </div>

      <h2 style={h2}>How long things are kept</h2>
      <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column' }}>
        {KEPT.map(([what, kept, remove]) => (
          <div
            key={what}
            style={{
              padding: '12px 0',
              borderBottom: '1px solid var(--border-row)',
              display: 'flex',
              flexDirection: 'column',
              gap: 3,
              font: '400 13.5px/1.5 var(--font-sans)',
            }}
          >
            <span style={{ color: 'var(--text-primary)' }}>{what}</span>
            <span style={{ color: 'var(--text-hint)' }}>
              {kept} <span style={{ color: 'var(--text-dim)' }}>· {remove}</span>
            </span>
          </div>
        ))}
      </div>

      <h2 style={h2}>Who else sees it</h2>
      <div style={p}>
        Amazon Web Services, in Sydney, runs all of it. Stripe handles payment on its own pages.
        The voice that reads your meditation comes from a text-to-speech provider that receives
        the finished script and nothing else.
      </div>

      <div
        style={{
          marginTop: 36,
          paddingBottom: 34,
          font: '400 12px var(--font-mono)',
          color: 'var(--text-dim)',
        }}
      >
        Last checked against the code: {LAST_CHECKED}
      </div>
    </div>
  )
}
