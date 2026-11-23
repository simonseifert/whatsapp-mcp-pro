import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from './api'
import type { DraftView, LiveEvent } from './api-types'
import * as live from './live'
import { useDraft } from './useDraft'

const JID = 'a@s.whatsapp.net'
const draft = (id: number, extra: Partial<DraftView> = {}): DraftView => ({
  id,
  chat_jid: JID,
  trigger_id: 'M1',
  status: 'ready',
  alternatives: ['Bok!'],
  error: null,
  quote: null,
  created_at_iso: '2026-11-23T10:00:00+00:00',
  ...extra,
})

describe('useDraft', () => {
  let emit: (event: LiveEvent) => void
  beforeEach(() => {
    vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-11-23T12:00:00Z'))
    vi.spyOn(live, 'subscribeLive').mockImplementation((listener) => {
      emit = listener
      return () => {}
    })
    vi.spyOn(api, 'fetchDraft').mockResolvedValue({ draft: draft(1), chat_jid: JID })
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('loads the current draft, applies draft events for this chat and clears on null', async () => {
    const { result } = renderHook(() => useDraft(JID))
    await waitFor(() => expect(result.current.draft?.id).toBe(1))
    act(() => emit({ name: 'draft', data: { chat_jid: JID, draft: draft(2, { alternatives: ['Hej', 'Bok'] }) } }))
    expect(result.current.draft?.alternatives).toEqual(['Hej', 'Bok'])
    act(() => emit({ name: 'draft', data: { chat_jid: 'other@s.whatsapp.net', draft: draft(9) } }))
    expect(result.current.draft?.id).toBe(2)
    act(() => emit({ name: 'draft', data: { chat_jid: JID, draft: null } }))
    expect(result.current.draft).toBeNull()
  })

  it('hides a draft older than 24 h, also while the client stays open', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { result } = renderHook(() => useDraft(JID))
    await waitFor(() => expect(result.current.draft?.id).toBe(1))
    vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-11-24T10:00:01Z'))
    act(() => vi.advanceTimersByTime(60_000))
    expect(result.current.draft).toBeNull()
  })

  it('keeps the canonical jid when there is no draft, so a live event under it still shows', async () => {
    vi.spyOn(api, 'fetchDraft').mockResolvedValue({ draft: null, chat_jid: JID })
    const { result } = renderHook(() => useDraft('+385910000001'))
    await waitFor(() => expect(api.fetchDraft).toHaveBeenCalled())
    await act(async () => {})
    act(() => emit({ name: 'draft', data: { chat_jid: JID, draft: draft(5) } }))
    expect(result.current.draft?.id).toBe(5)
  })

  it('ignores a draft event for a different chat', async () => {
    const { result } = renderHook(() => useDraft(JID))
    await waitFor(() => expect(result.current.draft?.id).toBe(1))
    act(() => emit({ name: 'draft', data: { chat_jid: 'other@s.whatsapp.net', draft: null } }))
    expect(result.current.draft?.id).toBe(1)
  })

  it('request() posts for a one-off draft and shows it; a 404 becomes a brief error', async () => {
    vi.spyOn(api, 'fetchDraft').mockResolvedValue({ draft: null, chat_jid: JID })
    const post = vi.spyOn(api, 'requestDraft').mockResolvedValueOnce(draft(3, { status: 'pending', alternatives: [] }))
    const { result } = renderHook(() => useDraft(JID))
    await waitFor(() => expect(post).not.toHaveBeenCalled())
    await act(async () => result.current.request())
    expect(post).toHaveBeenCalledWith(JID, undefined)
    expect(result.current.draft?.status).toBe('pending')
    post.mockRejectedValueOnce(new api.ApiError(404, 'Nothing to reply to'))
    await act(async () => result.current.request())
    expect(result.current.error).toBe('Nothing to reply to')
  })

  it('dismiss() removes the card at once and calls DELETE', async () => {
    const del = vi.spyOn(api, 'dismissDraft').mockResolvedValue(null)
    const { result } = renderHook(() => useDraft(JID))
    await waitFor(() => expect(result.current.draft?.id).toBe(1))
    act(() => result.current.dismiss())
    expect(result.current.draft).toBeNull()
    expect(del).toHaveBeenCalledWith(1)
  })
})
