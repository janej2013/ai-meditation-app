/**
 * The inline states of [Companion · errors]: each appears alone, in place of
 * a reply. No dialogs, no red, no icons.
 */
import type { CompanionError } from './useCompanion'

interface Props {
  error: Exclude<CompanionError, null>
  hasProposal: boolean
  onSendAgain: () => void
  onStartOver: () => void
  onAddCredits: () => void
  onTryStartAgain: () => void
}

export default function InlineError({
  error,
  hasProposal,
  onSendAgain,
  onStartOver,
  onAddCredits,
  onTryStartAgain,
}: Props) {
  if (error === 'model')
    return (
      <div className="inline-error">
        <div className="inline-error-text">Something went quiet on my side. Send that again?</div>
        <button className="pill-action" onClick={onSendAgain}>
          Send again
        </button>
      </div>
    )
  if (error === 'start')
    return (
      <div className="inline-error">
        <div className="inline-error-text">Couldn't start it just now. Try again?</div>
        <button className="pill-action" onClick={onTryStartAgain}>
          Try again
        </button>
      </div>
    )
  if (error === 'exhausted')
    return (
      <div className="inline-error">
        <div className="inline-error-text">
          {hasProposal
            ? "We've talked for a while. Start the meditation above, or begin a fresh conversation."
            : "We've talked for a while. Begin a fresh conversation whenever you like."}
        </div>
        <button className="pill-action" onClick={onStartOver}>
          Start over
        </button>
      </div>
    )
  return (
    <div className="nocredit-card">
      <div className="nocredit-text">No generations left</div>
      <button className="nocredit-link" onClick={onAddCredits}>
        Add credits
      </button>
    </div>
  )
}
