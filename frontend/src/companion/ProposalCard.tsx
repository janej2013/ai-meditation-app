/**
 * The proposal card ([Companion · proposal]): deliberately the heaviest thing
 * on the screen, because "Start the meditation" is the only tap in the whole
 * feature that spends a credit.
 */
import type { Proposal } from './useCompanion'

interface Props {
  proposal: Proposal
  credits: number | null
  briefOpen: boolean
  starting: boolean
  onOpenBrief: () => void
  onCloseBrief: () => void
  onStart: () => void
  onChange: () => void
}

export default function ProposalCard({
  proposal,
  credits,
  briefOpen,
  starting,
  onOpenBrief,
  onCloseBrief,
  onStart,
  onChange,
}: Props) {
  const cost = credits === null ? 'Uses 1 credit' : `Uses 1 credit · ${credits} left`
  return (
    <div className="proposal-card" aria-label="Proposed meditation">
      <div className="proposal-head">
        <div className="proposal-duration">{proposal.duration_minutes} min</div>
        <div className="proposal-cost">{cost}</div>
      </div>
      {briefOpen ? (
        <>
          <div className="proposal-brief">{proposal.brief}</div>
          <button className="proposal-link proposal-link-plain" onClick={onCloseBrief}>
            Hide the brief
          </button>
        </>
      ) : (
        <>
          {proposal.summary && <div className="proposal-summary">{proposal.summary}</div>}
          {proposal.brief && (
            <button className="proposal-link" onClick={onOpenBrief}>
              Read the brief
            </button>
          )}
        </>
      )}
      <div className="proposal-actions">
        <button className="btn-primary proposal-start" onClick={onStart} disabled={starting}>
          {starting ? 'Starting…' : 'Start the meditation'}
        </button>
        <button className="btn-ghost proposal-change" onClick={onChange} disabled={starting}>
          Change something
        </button>
      </div>
    </div>
  )
}
