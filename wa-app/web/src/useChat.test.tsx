import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from './api'
import type { ChatDetail, LiveEvent, MessageView, MessagesResponse } from './api-types'
import * as live from './live'
import { useChat } from './useChat'

const JID = 'a@s.whatsapp.net'
const msg = (id: string, iso: string): MessageView => ({ id, chat_jid: JID, timestamp_iso: iso, text: id }) as MessageView
const detail = { chat: { jid: JID, unread: 1 }, members_line: null } as unknown as ChatDetail

describe('useChat', () => {
  let emit: (event: LiveEvent) => void
  let release: () => void
  beforeEach(() => {
    vi.spyOn(live, 'subscribeLive').mockImplementation((listener) => {
      emit = listener
      return () => {}
    })
    const gate = new Promise<void>((resolve) => (release = resolve))
    vi.spyOn(api, 'apiFetch').mockImplementation(async (path: string) => {
      await gate
      if (path.endsWith('/messages')) {
        return { messages: [msg('m1', '2026-11-22T10:00:00+01:00')], has_more: false } satisfies MessagesResponse
      }
      return detail
    })
  })
  afterEach(() => vi.restoreAllMocks())

  it('applies a live message that arrived while the first load was still running', async () => {
    const arrivals: string[] = []
    const { result } = renderHook(() => useChat(JID, (m) => arrivals.push(m.id)))
    expect(result.current.status).toBe('loading')
    act(() => emit({ name: 'message', data: { chat_jid: JID, message: msg('m2', '2026-11-22T10:05:00+01:00') } }))
    await act(async () => release())
    await waitFor(() => expect(result.current.status).toBe('ready'))
    expect(result.current.messages.map((m) => m.id)).toEqual(['m1', 'm2'])
    expect(arrivals).toEqual(['m2'])
  })
})
