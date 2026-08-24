/**
 * The collection's data: cursor pagination that accumulates, deletion that is
 * optimistic (the card leaves immediately; a failed DELETE puts it back), and
 * a module-level count the home screen reads without loading the page.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { type DreamscapeItem, deleteDreamscape, listDreamscapes } from '../api/client'

// Derived from the first page and kept across navigations, so the home
// screen's entry line can render without a spinner: null = not known yet,
// and the line simply does not render until it is.
let cachedCount: number | null = null

export function resetDreamCountCache(): void {
  cachedCount = null
}

export function useDreamCount(): number | null {
  const [count, setCount] = useState<number | null>(cachedCount)
  useEffect(() => {
    if (cachedCount !== null) return
    let cancelled = false
    listDreamscapes()
      .then((page) => {
        cachedCount = page.items.length
        if (!cancelled) setCount(cachedCount)
      })
      .catch(() => {
        // Not signed in, offline: the entry line just stays absent.
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
  loadMore: () => void
  /** Optimistic; resolves false (and restores the card) if the API refused. */
  remove: (jobId: string) => Promise<boolean>
}

export function useDreamscapes(): Dreamscapes {
  const [items, setItems] = useState<DreamscapeItem[] | null>(null)
  const cursor = useRef<string | null>(null)
  const loading = useRef(false)
  const [hasMore, setHasMore] = useState(false)

  const fetchPage = useCallback(async () => {
    if (loading.current) return
    loading.current = true
    try {
      const page = await listDreamscapes(cursor.current ?? undefined)
      cursor.current = page.next_cursor
      setHasMore(page.next_cursor !== null)
      setItems((prev) => [...(prev ?? []), ...page.items])
      if (cachedCount === null) cachedCount = page.items.length
    } catch {
      // Leave items as they are; the page shows its quiet empty/loading state.
    } finally {
      loading.current = false
    }
  }, [])

  useEffect(() => {
    void fetchPage()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount-only load
  }, [])

  const remove = useCallback(async (jobId: string): Promise<boolean> => {
    let removed: DreamscapeItem | undefined
    let at = 0
    setItems((prev) => {
      if (!prev) return prev
      at = prev.findIndex((d) => d.job_id === jobId)
      removed = prev[at]
      return prev.filter((d) => d.job_id !== jobId)
    })
    if (cachedCount !== null) cachedCount = Math.max(0, cachedCount - 1)
    try {
      await deleteDreamscape(jobId)
      return true
    } catch {
      setItems((prev) => {
        if (!prev || !removed) return prev
        const back = [...prev]
        back.splice(Math.min(at, back.length), 0, removed)
        return back
      })
      if (cachedCount !== null) cachedCount += 1
      return false
    }
  }, [])

  return { items, hasMore, loadMore: () => void fetchPage(), remove }
}
