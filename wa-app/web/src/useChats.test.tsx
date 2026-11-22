import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from './api'
import type { ChatSummary, ChatsResponse } from './api-types'
import * as live from './live'
import { useChats } from './useChats'

const chat = (jid: string, iso: string, pinned = false) =>
  ({ jid, pinned, last_message: { timestamp_iso: iso } }) as unknown as ChatSummary
const A = chat('a', '2026-11-22T12:00:00+01:00', true)
const B = chat('b', '2026-11-22T11:00:00+01:00')
const C = chat('c', '2026-11-22T10:00:00+01:00')
const D = chat('d', '2026-11-22T09:00:00+01:00')

describe('useChats paging', () => {
  let calls: string[]
  beforeEach(() => {
    calls = []
    vi.spyOn(live, 'subscribeLive').mockReturnValue(() => {})
    vi.spyOn(api, 'apiFetch').mockImplementation(async (path: string) => {
      calls.push(path)
      const before = new URLSearchParams(path.split('?')[1]).get('before')
      const body: ChatsResponse = before
        ? { chats: [C, D], archived_count: 0, has_more: false }
        : { chats: [A, B], archived_count: 0, has_more: true }
      return body
    })
  })
  afterEach(() => vi.restoreAllMocks())

  it('loadMore asks for the chats older than the last one and appends them once', async () => {
    const { result } = renderHook(() => useChats('all', false, ''))
    await waitFor(() => expect(result.current.data?.chats).toHaveLength(2))
    expect(result.current.data?.has_more).toBe(true)
    await act(async () => {
      result.current.loadMore()
      result.current.loadMore() // a second call while one is out does nothing
    })
    await waitFor(() => expect(result.current.data?.chats.map((c) => c.jid)).toEqual(['a', 'b', 'c', 'd']))
    expect(result.current.data?.has_more).toBe(false)
    const paged = calls.filter((c) => c.includes('before='))
    expect(paged).toHaveLength(1)
    expect(decodeURIComponent(paged[0])).toContain('before=2026-11-22T11:00:00+01:00')
  })
})
