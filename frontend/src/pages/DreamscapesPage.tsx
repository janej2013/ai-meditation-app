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
import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { type DreamscapeItem } from '../api/client'
import { dreamThumb } from '../dreamscapes/thumb'
import { useDreamscapes } from '../dreamscapes/useDreamscapes'
import { wovenAgo } from '../dreamscapes/wovenAgo'

const SWIPE_PX = 36 // the prototype's open/close threshold

function title(d: DreamscapeItem): string {
  return d.keywords?.length ? d.keywords.join(' · ') : (d.mood_excerpt ?? 'A quiet session')
}

export default function DreamscapesPage() {
  const navigate = useNavigate()
  const { items, hasMore, loadMore, remove } = useDreamscapes()
  const [swipeId, setSwipeId] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<DreamscapeItem | null>(null)
  const [failedDelete, setFailedDelete] = useState(false)
  const [fade, setFade] = useState(0)
  const downX = useRef<number | null>(null)

  useEffect(() => {
    const t = setTimeout(() => setFade(1), 40)
    return () => clearTimeout(t)
  }, [])

  const open = (d: DreamscapeItem) => {
    navigate(`/player/${d.job_id}`, {
      state: {
        from: 'dreamscapes',
        keywords: d.keywords,
        moodExcerpt: d.mood_excerpt,
        createdAt: d.created_at,
        pic: d.source_type === 'picture',
      },
    })
  }

  const onUp = (d: DreamscapeItem, e: React.PointerEvent) => {
    const dx = e.clientX - (downX.current ?? e.clientX)
    if (dx < -SWIPE_PX) setSwipeId(d.job_id)
    else if (dx > SWIPE_PX) setSwipeId(null)
    else if (swipeId === d.job_id) setSwipeId(null)
    else open(d)
  }

  const confirmDelete = async () => {
    if (!confirming) return
    const target = confirming
    setConfirming(null)
    setSwipeId(null)
    setFailedDelete(false)
    if (!(await remove(target.job_id))) setFailedDelete(true)
  }

  return (
    <div
      className="screen"
      style={{
        padding: 0,
        transition: 'opacity 1s ease',
        opacity: fade,
        position: 'relative',
      }}
    >
      <div
        style={{
          flex: 'none',
          padding: '18px 30px 0',
          display: 'flex',
          alignItems: 'center',
          gap: 14,
        }}
      >
        <button
          onClick={() => navigate('/')}
          style={{
            background: 'none',
            border: 'none',
            padding: '6px 8px',
            margin: '-6px -8px',
            fontSize: 15,
            color: 'var(--text-secondary)',
            cursor: 'pointer',
          }}
        >
          ←
        </button>
        <div style={{ font: '300 24px var(--font-sans)', color: 'var(--text-primary)' }}>
          Your dreamscapes
        </div>
      </div>

      {items !== null && items.length > 0 && (
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
              <div
                key={d.job_id}
                style={{
                  position: 'relative',
                  borderRadius: 20,
                  overflow: 'hidden',
                  flex: 'none',
                }}
              >
                <div
                  style={{
                    position: 'absolute',
                    inset: 0,
                    display: 'flex',
                    justifyContent: 'flex-end',
                  }}
                >
                  <button
                    onClick={() => setConfirming(d)}
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
                  onPointerUp={(e) => onUp(d, e)}
                  style={{
                    position: 'relative',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 15,
                    padding: '15px 17px',
                    borderRadius: 20,
                    background: 'oklch(0.265 0.032 265 / 0.82)',
                    backdropFilter: 'blur(16px)',
                    cursor: 'pointer',
                    transition: 'transform .26s ease, background .2s',
                    transform: swipeId === d.job_id ? 'translateX(-88px)' : 'translateX(0)',
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
                      backgroundImage: dreamThumb(d.job_id),
                    }}
                  />
                  <div
                    style={{ minWidth: 0, display: 'flex', flexDirection: 'column', gap: 6 }}
                  >
                    <div
                      style={{
                        font: '400 15.5px/1.35 var(--font-sans)',
                        color: 'var(--text-primary)',
                        textWrap: 'pretty',
                      }}
                    >
                      {title(d)}
                    </div>
                    <div
                      style={{
                        font: '400 11.5px var(--font-mono)',
                        color: 'oklch(0.720 0.018 275)',
                      }}
                    >
                      {d.duration_minutes ?? '–'} min ·{' '}
                      {d.created_at ? wovenAgo(new Date(d.created_at)) : 'woven once'}
                    </div>
                  </div>
                </div>
              </div>
            ))}
            {hasMore && (
              <button className="btn-ghost" style={{ padding: '12px 0' }} onClick={loadMore}>
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

      {items !== null && items.length === 0 && (
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
          <button
            onClick={() => navigate('/')}
            style={{
              background: 'none',
              border: 'none',
              padding: '8px 3px',
              margin: '-8px -3px',
              font: '400 13px var(--font-sans)',
              color: 'oklch(0.775 0.018 275)',
              borderBottom: '1px solid oklch(0.72 0.02 275 / 0.24)',
              cursor: 'pointer',
            }}
          >
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
              {title(confirming)} will drift out of your collection.
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
                onClick={() => setConfirming(null)}
                style={{
                  background: 'none',
                  border: 'none',
                  padding: '14px 0',
                  fontSize: 13,
                  color: 'oklch(0.760 0.018 275)',
                  cursor: 'pointer',
                }}
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
