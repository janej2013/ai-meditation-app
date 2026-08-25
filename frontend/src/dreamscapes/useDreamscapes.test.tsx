/**
 * The collection hook: accumulating pagination, optimistic delete with
 * rollback, the signed-out signal, and the module-level count the home
 * screen reads.
 */
import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, listDreamscapes: vi.fn(), deleteDreamscape: vi.fn() }
})

import {
  type DreamscapeItem,
  type DreamscapeList,
  NotSignedInError,
  deleteDreamscape,
  listDreamscapes,
} from '../api/client'
import { invalidateDreamCount, useDreamCount, useDreamscapes } from './useDreamscapes'

function item(id: string): DreamscapeItem {
  return {
    job_id: id,
    keywords: null,
    mood_excerpt: 'calm',
    duration_minutes: 10,
    source_type: 'text',
    created_at: '2026-08-20T00:00:00+00:00',
  }
}

function page(ids: string[], next: string | null = null, total = ids.length): DreamscapeList {
  return { items: ids.map(item), next_cursor: next, total }
}

beforeEach(() => {
  vi.mocked(listDreamscapes).mockReset()
  vi.mocked(deleteDreamscape).mockReset()
  invalidateDreamCount()
})

describe('useDreamscapes', () => {
  it('loads the first page and accumulates the next', async () => {
    vi.mocked(listDreamscapes)
      .mockResolvedValueOnce(page(['a', 'b'], 'c1', 3))
      .mockResolvedValueOnce(page(['c'], null, 3))

    const { result } = renderHook(() => useDreamscapes())
    await waitFor(() => expect(result.current.items).toHaveLength(2))
    expect(result.current.hasMore).toBe(true)

    await act(() => result.current.loadMore())
    await waitFor(() => expect(result.current.items).toHaveLength(3))
    expect(vi.mocked(listDreamscapes)).toHaveBeenLastCalledWith('c1')
    expect(result.current.hasMore).toBe(false)
  })

  it('removes optimistically and keeps the removal when the API agrees', async () => {
    vi.mocked(listDreamscapes).mockResolvedValue(page(['a', 'b']))
    vi.mocked(deleteDreamscape).mockResolvedValue(undefined)

    const { result } = renderHook(() => useDreamscapes())
    await waitFor(() => expect(result.current.items).toHaveLength(2))

    let ok = false
    await act(async () => {
      ok = await result.current.remove('a')
    })
    expect(ok).toBe(true)
    expect(result.current.items?.map((d) => d.job_id)).toEqual(['b'])
  })

  it('puts the card back where it was when the DELETE fails', async () => {
    vi.mocked(listDreamscapes).mockResolvedValue(page(['a', 'b', 'c']))
    vi.mocked(deleteDreamscape).mockRejectedValue(new Error('500'))

    const { result } = renderHook(() => useDreamscapes())
    await waitFor(() => expect(result.current.items).toHaveLength(3))

    let ok = true
    await act(async () => {
      ok = await result.current.remove('b')
    })
    expect(ok).toBe(false)
    expect(result.current.items?.map((d) => d.job_id)).toEqual(['a', 'b', 'c'])
  })

  it('flags failed (not signedOut) when the first page cannot be loaded', async () => {
    vi.mocked(listDreamscapes).mockRejectedValue(new Error('500'))

    const { result } = renderHook(() => useDreamscapes())

    await waitFor(() => expect(result.current.failed).toBe(true))
    expect(result.current.signedOut).toBe(false)
    expect(result.current.items).toBeNull()
  })

  it('flags signedOut so the page can redirect', async () => {
    vi.mocked(listDreamscapes).mockRejectedValue(new NotSignedInError())

    const { result } = renderHook(() => useDreamscapes())

    await waitFor(() => expect(result.current.signedOut).toBe(true))
    expect(result.current.items).toBeNull()
  })
})

describe('useDreamCount', () => {
  it('reports the API total and renders nothing while unknown', async () => {
    let resolve!: (v: DreamscapeList) => void
    vi.mocked(listDreamscapes).mockReturnValue(
      new Promise((r) => {
        resolve = r
      }),
    )

    const { result } = renderHook(() => useDreamCount())
    expect(result.current).toBeNull()

    // The API's total, not the page length: 25 dreams read as 25, not 20.
    resolve(page(['a', 'b'], 'more', 25))
    await waitFor(() => expect(result.current).toBe(25))
  })

  it('stays null (no crash, no retry storm) when the request fails', async () => {
    vi.mocked(listDreamscapes).mockRejectedValue(new Error('offline'))
    const { result } = renderHook(() => useDreamCount())
    await waitFor(() => expect(vi.mocked(listDreamscapes)).toHaveBeenCalled())
    expect(result.current).toBeNull()
  })

  it('paints the last known count at once, then revalidates', async () => {
    vi.mocked(listDreamscapes).mockResolvedValueOnce(page(['a'], null, 1))
    const first = renderHook(() => useDreamCount())
    await waitFor(() => expect(first.result.current).toBe(1))

    // A session completed elsewhere: the next mount shows 1 immediately (no
    // spinner) and settles on the server's 2 without anyone invalidating.
    vi.mocked(listDreamscapes).mockResolvedValueOnce(page(['a', 'b'], null, 2))
    const second = renderHook(() => useDreamCount())
    expect(second.result.current).toBe(1)
    await waitFor(() => expect(second.result.current).toBe(2))
  })

  it('forgets the count on invalidation, so a new account never sees the last one', async () => {
    vi.mocked(listDreamscapes).mockResolvedValueOnce(page(['a'], null, 1))
    const first = renderHook(() => useDreamCount())
    await waitFor(() => expect(first.result.current).toBe(1))

    invalidateDreamCount()
    vi.mocked(listDreamscapes).mockReturnValue(new Promise(() => {}))
    const second = renderHook(() => useDreamCount())
    expect(second.result.current).toBeNull()
  })
})
