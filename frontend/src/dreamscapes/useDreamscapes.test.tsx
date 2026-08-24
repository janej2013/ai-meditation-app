/**
 * The collection hook: accumulating pagination, optimistic delete with
 * rollback, and the module-level count cache the home screen reads.
 */
import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, listDreamscapes: vi.fn(), deleteDreamscape: vi.fn() }
})

import { type DreamscapeItem, deleteDreamscape, listDreamscapes } from '../api/client'
import { resetDreamCountCache, useDreamCount, useDreamscapes } from './useDreamscapes'

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

beforeEach(() => {
  vi.mocked(listDreamscapes).mockReset()
  vi.mocked(deleteDreamscape).mockReset()
  resetDreamCountCache()
})

describe('useDreamscapes', () => {
  it('loads the first page and accumulates the next', async () => {
    vi.mocked(listDreamscapes)
      .mockResolvedValueOnce({ items: [item('a'), item('b')], next_cursor: 'c1' })
      .mockResolvedValueOnce({ items: [item('c')], next_cursor: null })

    const { result } = renderHook(() => useDreamscapes())
    await waitFor(() => expect(result.current.items).toHaveLength(2))
    expect(result.current.hasMore).toBe(true)

    act(() => result.current.loadMore())
    await waitFor(() => expect(result.current.items).toHaveLength(3))
    expect(vi.mocked(listDreamscapes)).toHaveBeenLastCalledWith('c1')
    expect(result.current.hasMore).toBe(false)
  })

  it('removes optimistically and keeps the removal when the API agrees', async () => {
    vi.mocked(listDreamscapes).mockResolvedValue({
      items: [item('a'), item('b')],
      next_cursor: null,
    })
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
    vi.mocked(listDreamscapes).mockResolvedValue({
      items: [item('a'), item('b'), item('c')],
      next_cursor: null,
    })
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
})

describe('useDreamCount', () => {
  it('derives the count from the first page and renders nothing while unknown', async () => {
    let resolve!: (v: { items: DreamscapeItem[]; next_cursor: string | null }) => void
    vi.mocked(listDreamscapes).mockReturnValue(
      new Promise((r) => {
        resolve = r
      }),
    )

    const { result } = renderHook(() => useDreamCount())
    expect(result.current).toBeNull()

    resolve({ items: [item('a'), item('b')], next_cursor: null })
    await waitFor(() => expect(result.current).toBe(2))
  })

  it('stays null (no crash, no retry storm) when the request fails', async () => {
    vi.mocked(listDreamscapes).mockRejectedValue(new Error('offline'))
    const { result } = renderHook(() => useDreamCount())
    await waitFor(() => expect(vi.mocked(listDreamscapes)).toHaveBeenCalled())
    expect(result.current).toBeNull()
  })
})
