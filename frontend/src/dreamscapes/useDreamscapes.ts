/**
 * The collection's data: cursor pagination that accumulates, deletion that is
 * optimistic (the card leaves immediately; a failed DELETE puts it back), and
 * a module-level count the home screen reads without loading the page.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  type DreamscapeItem,
  NotSignedInError,
  deleteDreamscape,
  listDreamscapes,
} from '../api/client'

// The API's `total` from the last page fetched, kept across navigations so
// the home screen's entry line renders without a spinner: null = not known,
// and the line simply does not render until it is. Invalidated when the
// collection can have changed behind our back -- a session completing, a
// sign-out -- so the next visit refetches.
let cachedCount: number | null = null

export function invalidateDreamCount(): void {
  cachedCount = null
}

export function useDreamCount(): number | null {
  const [count, setCount] = useState<number | null>(cachedCount)
  useEffect(() => {
    if (cachedCount !== null) return
    let cancelled = false
    listDreamscapes()
      .then((page) => {
        cachedCount = page.total
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
  /** The collection needs a signed-in user; the page redirects on this. */
  signedOut: boolean
  loadMore: () => Promise<void>
  /** Optimistic; resolves false (and restores the card) if the API refused. */
  remove: (jobId: string) => Promise<boolean>
}

export function useDreamscapes(): Dreamscapes {
  const [items, setItems] = useState<DreamscapeItem[] | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [signedOut, setSignedOut] = useState(false)
  // Mirrors `items` for code that needs the current list outside a render
  // (the rollback snapshot) without side effects inside a state updater.
  const itemsRef = useRef<DreamscapeItem[] | null>(null)
  const cursor = useRef<string | null>(null)
  const loading = useRef(false)

  const setList = useCallback((next: DreamscapeItem[] | null) => {
    itemsRef.current = next
    setItems(next)
  }, [])

  const fetchPage = useCallback(async () => {
    if (loading.current) return
    loading.current = true
    try {
      const page = await listDreamscapes(cursor.current ?? undefined)
      cursor.current = page.next_cursor
      cachedCount = page.total
      setHasMore(page.next_cursor !== null)
      setList([...(itemsRef.current ?? []), ...page.items])
    } catch (e) {
      if (e instanceof NotSignedInError) setSignedOut(true)
      // Otherwise leave the list as it is; the page shows its quiet state.
    } finally {
      loading.current = false
    }
  }, [setList])

  useEffect(() => {
    void fetchPage()
  }, [fetchPage])

  const remove = useCallback(
    async (jobId: string): Promise<boolean> => {
      const before = itemsRef.current ?? []
      const at = before.findIndex((d) => d.job_id === jobId)
      if (at === -1) return true
      setList(before.filter((d) => d.job_id !== jobId))
      if (cachedCount !== null) cachedCount = Math.max(0, cachedCount - 1)
      try {
        await deleteDreamscape(jobId)
        return true
      } catch {
        const back = [...(itemsRef.current ?? [])]
        back.splice(Math.min(at, back.length), 0, before[at])
        setList(back)
        if (cachedCount !== null) cachedCount += 1
        return false
      }
    },
    [setList],
  )

  return { items, hasMore, signedOut, loadMore: fetchPage, remove }
}
