/**
 * /companion -- the conversation screen (design: Companion Artboards.dc.html,
 * and the `isCompanion` block of the running prototype).
 *
 * Header, the thread, one line of tool activity while the runner works, the
 * reply streaming in with a breathing cursor, the proposal card when the
 * model has one, and an input bar pinned to the keyboard. Every state the
 * runner can produce has a place here; none of them is a dialog.
 */
import { useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { DEFAULT_BGM_TRACK, bgmUrl, mixer } from '../audio/mixer'
import InlineError from './InlineError'
import PlanRequired from './PlanRequired'
import ProposalCard from './ProposalCard'
import Thread from './Thread'
import { SHOW_TURNS_LEFT_AT, useCompanion } from './useCompanion'

export default function CompanionPage() {
  const navigate = useNavigate()
  const companion = useCompanion((jobId, duration) =>
    navigate(`/generating/${jobId}`, { state: { duration, pic: false } }),
  )
  const { state } = companion
  const inputRef = useRef<HTMLInputElement>(null)
  const threadRef = useRef<HTMLDivElement>(null)

  // Keep the newest line in view as replies stream in.
  useEffect(() => {
    const el = threadRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [state.thread, state.streamText, state.toolLine, state.proposal, state.compError])

  useEffect(() => {
    if (state.gate === 'signin') navigate('/signup', { replace: true, state: { resume: true } })
  }, [state.gate, navigate])

  if (state.gate === 'plan_required') return <PlanRequired />

  const empty =
    state.thread.length === 0 && !state.busy && !state.crisis && !state.compError && !state.proposal
  const canSend = state.draft.trim().length > 0 && !state.busy && !state.starting
  const showTurnsLeft = state.turnsLeft > 0 && state.turnsLeft <= SHOW_TURNS_LEFT_AT
  const placeholder = state.proposal ? 'or tell me what to change…' : 'tired but wired…'

  return (
    <div className="companion" aria-busy={state.starting}>
      <div className="companion-head">
        <div className="companion-head-left">
          <button className="btn-back-arrow" aria-label="Back" onClick={() => navigate('/')}>
            ←
          </button>
          <div className="companion-title">Companion</div>
        </div>
        {showTurnsLeft && (
          <div className="turns-left">
            {state.turnsLeft === 1 ? '1 reply left' : `${state.turnsLeft} replies left`}
          </div>
        )}
      </div>

      <div
        ref={threadRef}
        className={`companion-thread${empty ? ' companion-thread-empty' : ''}`}
        role="log"
        aria-live="polite"
      >
        {empty && (
          <div className="companion-empty">
            <div className="companion-opening">
              Tell me how tonight feels, or what you'd like the meditation to do.
            </div>
            {state.insightsCount > 0 && (
              <div className="companion-memory-line">
                I remember a few things you told me. You can see or clear them in Account.
              </div>
            )}
          </div>
        )}

        {state.gate === 'quota' && (
          <div className="inline-error">
            <div className="inline-error-text">
              That's every conversation for this month. The companion is back next month.
            </div>
          </div>
        )}

        <Thread messages={state.thread} />

        {state.toolLine && (
          <div className="tool-line" aria-live="polite">
            {state.toolLine}
          </div>
        )}

        {state.streaming && (
          <div className="msg-companion">
            {state.streamText}
            <span className="stream-cursor" aria-hidden />
          </div>
        )}

        {state.compError && (
          <InlineError
            error={state.compError}
            hasProposal={state.proposal !== null}
            onSendAgain={() => void companion.sendAgain()}
            onStartOver={companion.startOver}
            onAddCredits={() => navigate('/plans?plan=plan_pro')}
            onTryStartAgain={() => void companion.start()}
          />
        )}

        {state.proposal && !state.crisis && (
          <ProposalCard
            proposal={state.proposal}
            credits={state.credits}
            briefOpen={state.briefOpen}
            changing={state.changing}
            starting={state.starting}
            onOpenBrief={companion.openBrief}
            onCloseBrief={companion.closeBrief}
            onStart={() => {
              // As on Home's Begin: the music starts inside the tap (the only
              // place a mobile browser allows) and plays through the wait.
              void mixer.startAmbient(bgmUrl(DEFAULT_BGM_TRACK))
              void companion.start().then((ok) => {
                if (!ok) mixer.stopAmbient()
              })
            }}
            onChange={() => {
              companion.dismissError()
              companion.beginChange()
              const field = inputRef.current
              field?.focus()
              field?.scrollIntoView({ block: 'nearest' })
            }}
          />
        )}
      </div>

      <div className="companion-input">
        <input
          ref={inputRef}
          className="companion-field"
          value={state.draft}
          placeholder={placeholder}
          aria-label="Message"
          disabled={state.starting}
          onChange={(e) => companion.setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && canSend) void companion.send()
          }}
        />
        <button
          className="companion-send"
          aria-label="Send"
          disabled={!canSend}
          onClick={() => void companion.send()}
        >
          ↑
        </button>
      </div>
    </div>
  )
}
