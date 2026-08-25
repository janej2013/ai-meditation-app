/**
 * The collection's data: cursor pagination that accumulates, deletion that is
 * optimistic (the card leaves immediately; a failed DELETE puts it back), and
 * a count the home screen renders without a spinner.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  type DreamscapeItem,
  NotSignedInError,
  deleteDreamscape,
  listDreamscapes,
} from '../api/client'

// The API's `total` from the last page fetched. Stale-while-revalidate: the
// home screen paints the last known value at once (no spinner) and always
// refetches on mount, so a session that completed, a delete, or a different
// user signing in shows through on the next visit. null = nothing known yet,
// and the entry line simply does not render until it is.
let cachedCount: number | null = null

/** Forget the count outright -- on sign-out/sign-in, so it never carries
 * from one account to the next even for the moment before the refetch. */
export function invalidateDreamCount(): void {
  cachedCount = null
}

export function useDreamCount(): number | null {
  const [count, setCount] = useState<number | null>(cachedCount)
  useEffect(() => {
    let cancelled = false
    listDreamscapes()
      .then((page) => {
        cachedCount = page.total
        if (!cancelled) setCount(cachedCount)
      })
      .catch(() => {
        // Not signed in, offline: keep whatever was known.
      })
    return () => {
      cancelled = true
    }
  }, [])
  return count
}

export interface Dreamscapes {
  /** null until the first page arrives — render nothing meanwhile. */
  items: DreamscapeItem[] | null
  hasMore: boolean
  /** The first page could not be loaded (and it was not a sign-in problem). */
  failed: boolean
  /** The collection needs a signed-in user; the page redirects on this. */
  signedOut: boolean
  loadMore: () => Promise<void>
  /** Optimistic; resolves false (and restores the card) if the API refused. */
  remove: (jobId: string) => Promise<boolean>
}

export function useDreamscapes(): Dreamscapes {
  const [items, setItems] = useState<DreamscapeItem[] | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [failed, setFailed] = useState(false)
  const [signedOut, setSignedOut] = useState(false)
  const cursor = useRef<string | null>(null)
  const loading = useRef(false)

  const fetchPage = useCallback(async () => {
    if (loading.current) return
    loading.current = true
    setFailed(false)
    try {
      const page = await listDreamscapes(cursor.current ?? undefined)
      cursor.current = page.next_cursor
      cachedCount = page.total
      setHasMore(page.next_cursor !== null)
      setItems((prev) => [...(prev ?? []), ...page.items])
    } catch (e) {
      if (e instanceof NotSignedInError) setSignedOut(true)
      else setFailed(true)
    } finally {
      loading.current = false
    }
  }, [])

  useEffect(() => {
    void fetchPage()
  }, [fetchPage])

  const remove = useCallback(
    async (jobId: string): Promise<boolean> => {
      const before = items ?? []
      const at = before.findIndex((d) => d.job_id === jobId)
      if (at === -1) return true
      const gone = before[at]
      setItems((prev) => (prev ?? []).filter((d) => d.job_id !== jobId))
      try {
        await deleteDreamscape(jobId)
        invalidateDreamCount() // the home screen refetches rather than guessing
        return true
      } catch {
        setItems((prev) => {
          const back = [...(prev ?? [])]
          // Bounded in case a loadMore landed meanwhile.
          back.splice(Math.min(at, back.length), 0, gone)
          return back
        })
        return false
      }
    },
    [items],
  )

  return { items, hasMore, failed, signedOut, loadMore: fetchPage, remove }
}
