/**
 * The dreamscapes collection, straight from the prototype's dreams screen:
 * keyword-titled cards over seeded dot-field thumbnails, swipe-left to reveal
 * Delete, a bottom-sheet soft confirmation ("Let this dream go?"), a quiet
 * empty state, and the free-plan footer line (display only — the retention
 * policy itself is not implemented in this milestone).
 *
 * Deletion is optimistic: the card drifts out immediately; if the API
 * refuses, the card returns and a quiet line says so.
 */
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { type DreamscapeItem } from '../api/client'
import { dreamThumb } from '../dreamscapes/thumb'
import { dreamTitle } from '../dreamscapes/title'
import { useDreamscapes } from '../dreamscapes/useDreamscapes'
import { wovenAgo } from '../dreamscapes/wovenAgo'
import { useFadeIn } from '../hooks/useFadeIn'
import { useScene } from '../scene/SceneContext'

const SWIPE_PX = 36 // the prototype's open/close threshold

interface CardProps {
  dream: DreamscapeItem
  swiped: boolean
  onSwipe: (jobId: string | null) => void
  onOpen: (dream: DreamscapeItem) => void
  onDelete: (dream: DreamscapeItem) => void
}

/**
 * One card. Memoised so a swipe on one card does not rebuild every other
 * card's thumbnail (34 gradients each) and date on the way through.
 */
const DreamCard = memo(function DreamCard({
  dream,
  swiped,
  onSwipe,
  onOpen,
  onDelete,
}: CardProps) {
  const downX = useRef<number | null>(null)
  const thumb = useMemo(() => dreamThumb(dream.job_id), [dream.job_id])
  const when = dream.created_at ? wovenAgo(new Date(dream.created_at)) : 'woven once'

  const onUp = (e: React.PointerEvent) => {
    const dx = e.clientX - (downX.current ?? e.clientX)
    if (dx < -SWIPE_PX) onSwipe(dream.job_id)
    else if (dx > SWIPE_PX) onSwipe(null)
    else if (swiped) onSwipe(null)
    else onOpen(dream)
  }

  return (
    <div style={{ position: 'relative', borderRadius: 20, overflow: 'hidden', flex: 'none' }}>
      <div
        style={{ position: 'absolute', inset: 0, display: 'flex', justifyContent: 'flex-end' }}
      >
        <button
          onClick={() => onDelete(dream)}
          style={{
            width: 88,
            border: 'none',
            background: 'oklch(0.36 0.055 20 / 0.85)',
            font: '400 12.5px var(--font-sans)',
            color: 'oklch(0.94 0.020 20)',
            cursor: 'pointer',
          }}
        >
          Delete
        </button>
      </div>
      <div
        onPointerDown={(e) => {
          downX.current = e.clientX
        }}
        onPointerUp={onUp}
        style={{
          position: 'relative',
          display: 'flex',
          alignItems: 'center',
          gap: 15,
          padding: '15px 17px',
          borderRadius: 20,
          // 0.82 alpha over the dim cloud reads the same as the prototype's
          // blurred glass, without N live backdrop blurs per frame while
          // the cloud animates underneath a scrolling list.
          background: 'oklch(0.265 0.032 265 / 0.82)',
          cursor: 'pointer',
          transition: 'transform .26s ease, background .2s',
          transform: swiped ? 'translateX(-88px)' : 'translateX(0)',
          touchAction: 'pan-y',
        }}
      >
        <div
          style={{
            width: 56,
            height: 56,
            flex: 'none',
            borderRadius: 15,
            backgroundColor: 'oklch(0.215 0.030 265)',
            backgroundImage: thumb,
          }}
        />
        <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
          <div
            style={{
              font: '400 15.5px/1.35 var(--font-sans)',
              color: 'var(--text-primary)',
              textWrap: 'pretty',
            }}
          >
            {dreamTitle(dream.keywords, dream.mood_excerpt)}
          </div>
          <div style={{ font: '400 11.5px var(--font-mono)', color: 'oklch(0.720 0.018 275)' }}>
            {dream.duration_minutes ?? '–'} min · {when}
          </div>
        </div>
      </div>
    </div>
  )
})

export default function DreamscapesPage() {
  const navigate = useNavigate()
  const { items, hasMore, failed, signedOut, loadMore, remove } = useDreamscapes()
  const [swipeId, setSwipeId] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<DreamscapeItem | null>(null)
  const [failedDelete, setFailedDelete] = useState(false)
  const fadeIn = useFadeIn()
  const { setCloudSrc, setDissolve } = useScene()

  useEffect(() => {
    if (signedOut) navigate('/signup', { replace: true })
  }, [signedOut, navigate])

  // The collection rests on the procedural cloud: coming back from a picture
  // dreamscape's player must not leave that picture behind the cards.
  useEffect(() => {
    setCloudSrc('')
    setDissolve(1)
  }, [setCloudSrc, setDissolve])

  // Stable, or every card re-renders (and rebuilds its thumbnail) on each
  // swipe -- DreamCard's memo only holds while its props keep identity.
  const open = useCallback(
    (d: DreamscapeItem) => {
      navigate(`/player/${d.job_id}`, {
        state: {
          from: 'dreamscapes',
          keywords: d.keywords,
          moodExcerpt: d.mood_excerpt,
          createdAt: d.created_at,
          pic: d.source_type === 'picture',
        },
      })
    },
    [navigate],
  )

  const confirmDelete = async () => {
    if (!confirming) return
    const target = confirming
    setConfirming(null)
    setSwipeId(null)
    setFailedDelete(false)
    if (!(await remove(target.job_id))) setFailedDelete(true)
  }

  // Empty only when there is truly nothing left -- deleting every loaded
  // card while more pages exist is not "no dreamscapes yet".
  const empty = items !== null && items.length === 0 && !hasMore

  return (
    <div className="screen" style={{ padding: 0, ...fadeIn }}>
      <div
        style={{
          flex: 'none',
          padding: '18px 30px 0',
          display: 'flex',
          alignItems: 'center',
          gap: 14,
        }}
      >
        <button className="btn-back" style={{ fontSize: 15 }} onClick={() => navigate('/')}>
          ←
        </button>
        <div style={{ font: '300 24px var(--font-sans)', color: 'var(--text-primary)' }}>
          Your dreamscapes
        </div>
      </div>

      {items !== null && !empty && (
        <>
          <div
            style={{
              flex: 1,
              minHeight: 0,
              overflowY: 'auto',
              padding: '22px 22px 6px',
              display: 'flex',
              flexDirection: 'column',
              gap: 11,
            }}
          >
            {items.map((d) => (
              <DreamCard
                key={d.job_id}
                dream={d}
                swiped={swipeId === d.job_id}
                onSwipe={setSwipeId}
                onOpen={open}
                onDelete={setConfirming}
              />
            ))}
            {hasMore && (
              <button
                className="btn-ghost"
                style={{ padding: '12px 0' }}
                onClick={() => void loadMore()}
              >
                More dreamscapes
              </button>
            )}
          </div>
          <div
            style={{
              flex: 'none',
              padding: '14px 30px 4px',
              font: '400 11.5px/1.6 var(--font-sans)',
              color: 'oklch(0.700 0.018 275)',
              textWrap: 'pretty',
            }}
          >
            Free plan keeps your latest 3 dreamscapes — Plus keeps them all.
          </div>
        </>
      )}

      {failed && items === null && (
        <div
          style={{
            flex: 1,
            minHeight: 0,
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 20,
            padding: '0 44px',
          }}
        >
          <div
            style={{
              font: '300 19px/1.6 var(--font-sans)',
              color: 'oklch(0.880 0.016 275)',
              textAlign: 'center',
              textWrap: 'pretty',
            }}
          >
            Your dreamscapes could not be reached.
          </div>
          <button className="dream-entry" onClick={() => void loadMore()}>
            Try again
          </button>
        </div>
      )}

      {empty && (
        <div
          style={{
            flex: 1,
            minHeight: 0,
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 20,
            padding: '0 44px',
          }}
        >
          <div
            style={{
              font: '300 19px/1.6 var(--font-sans)',
              color: 'oklch(0.880 0.016 275)',
              textAlign: 'center',
              textWrap: 'pretty',
            }}
          >
            No dreamscapes yet. Your first dream awaits.
          </div>
          <button className="dream-entry" onClick={() => navigate('/')}>
            Begin one
          </button>
        </div>
      )}

      {failedDelete && (
        <div
          style={{
            position: 'absolute',
            left: 30,
            right: 30,
            bottom: 24,
            textAlign: 'center',
            font: '400 12.5px var(--font-sans)',
            color: 'oklch(0.86 0.05 20)',
          }}
        >
          That dream would not let go — it is back in your collection.
        </div>
      )}

      {confirming && (
        <div
          style={{
            position: 'absolute',
            inset: 0,
            zIndex: 4,
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'flex-end',
            background: 'oklch(0.145 0.024 265 / 0.6)',
            backdropFilter: 'blur(3px)',
          }}
        >
          <div
            style={{
              borderRadius: '26px 26px 0 0',
              padding: '30px 30px 34px',
              background: 'oklch(0.255 0.032 265)',
              display: 'flex',
              flexDirection: 'column',
              gap: 8,
            }}
          >
            <div style={{ font: '300 21px var(--font-sans)', color: 'var(--text-primary)' }}>
              Let this dream go?
            </div>
            <div
              style={{ font: '400 13px/1.6 var(--font-sans)', color: 'oklch(0.760 0.018 275)' }}
            >
              {dreamTitle(confirming.keywords, confirming.mood_excerpt)} will drift out of your
              collection.
            </div>
            <div style={{ marginTop: 18, display: 'flex', flexDirection: 'column', gap: 6 }}>
              <button
                onClick={() => void confirmDelete()}
                style={{
                  height: 54,
                  border: 'none',
                  borderRadius: 27,
                  background: 'var(--btn-primary-bg)',
                  fontSize: 15.5,
                  fontWeight: 500,
                  color: 'var(--btn-primary-fg)',
                  cursor: 'pointer',
                }}
              >
                Let go
              </button>
              <button
                className="btn-ghost"
                style={{ padding: '14px 0' }}
                onClick={() => setConfirming(null)}
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
