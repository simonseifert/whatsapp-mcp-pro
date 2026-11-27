import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { ChatDetail, LiveEvent, MessageView } from '../api-types'
import * as live from '../live'
import { resetOutbox } from '../outbox'
import { Chat } from './Chat'

const ANA = '385910000001@s.whatsapp.net'
const ago = (seconds: number) => new Date(Date.now() - seconds * 1000).toISOString()
const message = (id: string, text: string | null, extra: Partial<MessageView> = {}): MessageView => ({
  id,
  chat_jid: ANA,
  sender_jid: ANA,
  sender_name: 'Ana',
  from_me: true,
  timestamp_iso: ago(60),
  text,
  media: null,
  reply_to: null,
  forwarded: false,
  edited: false,
  deleted: false,
  system: false,
  reactions: [],
  poll: null,
  contact_cards: null,
  actionable: true,
  ticks: 'sent',
  ...extra,
})
const detail = {
  chat: { jid: ANA, name: 'Ana', kind: 'private', unread: 0, marked_unread: false, avatar_url: null },
  members_line: null,
} as unknown as ChatDetail

describe('Chat: message actions', () => {
  const listeners = new Set<(e: LiveEvent) => void>()
  const emit = (e: LiveEvent) => listeners.forEach((l) => l(e))
  let current: MessageView[]
  beforeEach(() => {
    resetOutbox()
    current = [message('M1', 'Vidimo se u 7'), message('M2', 'old', { timestamp_iso: ago(20 * 60) })]
    listeners.clear()
    vi.spyOn(live, 'subscribeLive').mockImplementation((l) => {
      listeners.add(l)
      return () => listeners.delete(l)
    })
    vi.spyOn(api, 'apiFetch').mockImplementation(async (path: string) => {
      const one = /\/messages\/([^/?]+)$/.exec(path)
      if (one) return { message: current.find((m) => m.id === one[1]) }
      if (path.includes('/messages')) return { messages: current, has_more: false }
      if (path.startsWith('/api/chats?')) return { chats: [], archived_count: 0, has_more: false }
      return detail
    })
  })
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  const openMenu = async (text: string) => {
    render(
      <MemoryRouter>
        <Chat jid={ANA} />
      </MemoryRouter>,
    )
    const bubble = (await screen.findByText(text)).closest('.msg') as HTMLElement
    fireEvent.contextMenu(bubble)
  }

  it('Edit puts the text in the composer, saves it through the edit route and the bubble updates from the live event', async () => {
    const edit = vi.spyOn(api, 'editMessage').mockResolvedValue({ ok: true, message_id: null })
    await openMenu('Vidimo se u 7')
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    const box = screen.getByLabelText('Message') as HTMLTextAreaElement
    expect(box.value).toBe('Vidimo se u 7')
    fireEvent.change(box, { target: { value: 'Vidimo se u 8' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save edit' }))
    await waitFor(() => expect(edit).toHaveBeenCalledWith(ANA, 'M1', 'Vidimo se u 8', expect.any(String)))
    await waitFor(() => expect(document.querySelector('.composer')?.getAttribute('data-editing')).toBe('false'))
    // The bridge applied the edit: the poller's "changed" event makes the chat refetch that one message.
    current = [message('M1', 'Vidimo se u 8', { edited: true }), current[1]]
    act(() => emit({ name: 'message_changed', data: { chat_jid: ANA, id: 'M1' } }))
    expect(await screen.findByText('Vidimo se u 8')).toBeTruthy()
    expect(screen.getAllByText('Edited').length).toBeGreaterThan(0)
  })

  it('has no Edit for a text sent more than 15 minutes ago', async () => {
    await openMenu('old')
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Delete for everyone' })).toBeTruthy()
  })

  it('a refused edit (too late) shows the reason and keeps the box', async () => {
    vi.spyOn(api, 'editMessage').mockResolvedValue({ ok: false, error: api.TOO_LATE, retryable: false })
    await openMenu('Vidimo se u 7')
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'changed' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save edit' }))
    expect((await screen.findByRole('alert')).textContent).toContain('Too late')
    expect((screen.getByLabelText('Message') as HTMLTextAreaElement).value).toBe('changed')
  })

  it('Delete for everyone asks first, then revokes, and the live event turns the bubble into "You deleted this message"', async () => {
    const revoke = vi.spyOn(api, 'revokeMessage').mockResolvedValue({ ok: true, message_id: null })
    await openMenu('Vidimo se u 7')
    fireEvent.click(screen.getByRole('button', { name: 'Delete for everyone' }))
    expect(revoke).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(revoke).toHaveBeenCalledWith(ANA, 'M1', expect.any(String)))
    current = [message('M1', null, { deleted: true }), current[1]]
    act(() => emit({ name: 'message_changed', data: { chat_jid: ANA, id: 'M1' } }))
    expect(await screen.findByText('You deleted this message')).toBeTruthy()
  })

  it('Star marks the bubble at once and Unstar takes it back; a failure undoes it with the reason', async () => {
    const star = vi.spyOn(api, 'starMessage').mockResolvedValueOnce({ ok: true, message_id: null })
    await openMenu('Vidimo se u 7')
    fireEvent.click(screen.getByRole('button', { name: 'Star' }))
    await waitFor(() => expect(star).toHaveBeenCalledWith(ANA, 'M1', true))
    expect(screen.getAllByLabelText('Starred').length).toBeGreaterThan(0)
    current = [message('M1', 'Vidimo se u 7', { starred: true }), current[1]] // what the server now says
    act(() => emit({ name: 'message_changed', data: { chat_jid: ANA, id: 'M1' } }))
    await waitFor(() => expect(screen.getAllByLabelText('Starred').length).toBeGreaterThan(0))
    star.mockResolvedValueOnce({ ok: false, error: 'Not sent: bridge didn\'t answer', retryable: true })
    fireEvent.contextMenu((screen.getByText('Vidimo se u 7').closest('.msg')) as HTMLElement)
    fireEvent.click(screen.getByRole('button', { name: 'Unstar' }))
    await waitFor(() => expect(star).toHaveBeenLastCalledWith(ANA, 'M1', false))
    expect((await screen.findByRole('alert')).textContent).toContain("bridge didn't answer")
    expect(screen.getAllByLabelText('Starred').length).toBeGreaterThan(0) // the failed unstar did not take
  })

  it('Forward opens the sheet for that message', async () => {
    await openMenu('Vidimo se u 7')
    fireEvent.click(screen.getByRole('button', { name: 'Forward' }))
    expect(await screen.findByRole('dialog', { name: 'Forward to' })).toBeTruthy()
  })
})
