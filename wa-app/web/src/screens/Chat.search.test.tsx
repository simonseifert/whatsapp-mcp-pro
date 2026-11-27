import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { ChatDetail, MessageView, SearchHit } from '../api-types'
import * as live from '../live'
import { resetOutbox } from '../outbox'
import { Chat } from './Chat'

const ANA = '385910000001@s.whatsapp.net'
const message = (id: string, text: string, minute: number): MessageView => ({
  id,
  chat_jid: ANA,
  sender_jid: ANA,
  sender_name: 'Ana',
  from_me: false,
  timestamp_iso: `2026-11-20T10:${String(minute).padStart(2, '0')}:00+00:00`,
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
  ticks: null,
})
const detail = {
  chat: { jid: ANA, name: 'Ana', kind: 'private', unread: 0, marked_unread: false, avatar_url: null },
  members_line: null,
} as unknown as ChatDetail
const hit = (id: string): SearchHit => ({
  chat_jid: ANA,
  chat_name: 'Ana',
  message_id: id,
  ts: '2026-11-20T10:00:00+00:00',
  sender_name: 'Ana',
  snippet: '[x]',
})

function Where() {
  const l = useLocation()
  return <p data-testid="where">{l.pathname + l.search}</p>
}
const flashed = () => document.querySelector('.msg.flash')?.getAttribute('data-message-id')

describe('Chat: jumping to a message and searching inside the chat', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>
  beforeEach(() => {
    resetOutbox()
    vi.spyOn(live, 'subscribeLive').mockImplementation(() => () => {})
    fetchSpy = vi.spyOn(api, 'apiFetch').mockImplementation(async (path: string) => {
      if (path.includes('around=')) {
        return { messages: [message('O1', 'old one', 1), message('O2', 'the target', 2), message('O3', 'after', 3)], has_more: true, has_newer: true }
      }
      if (path.endsWith('/messages')) {
        return { messages: [message('N1', 'newest one', 50), message('N2', 'newest two', 51)], has_more: false }
      }
      return detail
    })
  })
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('opens a window around the message from ?around=, highlights it, and offers the way back to the latest', async () => {
    render(
      <MemoryRouter initialEntries={[`/chat/${encodeURIComponent(ANA)}?around=O2`]}>
        <Where />
        <Chat jid={ANA} />
      </MemoryRouter>,
    )
    await screen.findByText('the target')
    expect(fetchSpy).toHaveBeenCalledWith(`/api/chats/${encodeURIComponent(ANA)}/messages?around=O2`)
    await waitFor(() => expect(flashed()).toBe('O2'))
    fireEvent.click(screen.getByRole('button', { name: 'Jump to latest' }))
    await screen.findByText('newest two')
    expect(screen.getByTestId('where').textContent).toBe(`/chat/${encodeURIComponent(ANA)}`)
    expect(screen.queryByRole('button', { name: 'Jump to latest' })).toBeNull()
  })

  it('sending from a window around an old message jumps to the latest first', async () => {
    render(
      <MemoryRouter initialEntries={[`/chat/${encodeURIComponent(ANA)}?around=O2`]}>
        <Where />
        <Chat jid={ANA} />
      </MemoryRouter>,
    )
    await screen.findByText('the target')
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Bok' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe(`/chat/${encodeURIComponent(ANA)}`))
  })

  it('the highlight fades after a moment', async () => {
    render(
      <MemoryRouter initialEntries={[`/chat/x?around=O2`]}>
        <Chat jid={ANA} />
      </MemoryRouter>,
    )
    await waitFor(() => expect(flashed()).toBe('O2'))
    await waitFor(() => expect(flashed()).toBeUndefined(), { timeout: 3000 })
  })

  it('in-chat search steps through the hits, older with Up and newer with Down', async () => {
    const search = vi.spyOn(api, 'searchMessages').mockResolvedValue({ hits: [hit('N2'), hit('N1')], next: null })
    render(
      <MemoryRouter>
        <Chat jid={ANA} />
      </MemoryRouter>,
    )
    await screen.findByText('newest two')
    fireEvent.click(screen.getByRole('button', { name: 'Chat menu' }))
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search in this chat' }), { target: { value: 'newest' } })
    await waitFor(() => expect(search).toHaveBeenCalledWith('newest', { chat: ANA }))
    await screen.findByText('1 of 2')
    await waitFor(() => expect(flashed()).toBe('N2'))
    fireEvent.click(screen.getByRole('button', { name: 'Older result' }))
    await screen.findByText('2 of 2')
    await waitFor(() => expect(flashed()).toBe('N1'))
    expect((screen.getByRole('button', { name: 'Older result' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Newer result' }))
    await screen.findByText('1 of 2')
    await waitFor(() => expect(flashed()).toBe('N2'))
    fireEvent.click(screen.getByRole('button', { name: 'Close search' }))
    expect(screen.queryByRole('searchbox', { name: 'Search in this chat' })).toBeNull()
  })

  it('a hit that is not loaded reopens the chat around it', async () => {
    vi.spyOn(api, 'searchMessages').mockResolvedValue({ hits: [hit('O2')], next: null })
    render(
      <MemoryRouter initialEntries={[`/chat/${encodeURIComponent(ANA)}`]}>
        <Where />
        <Chat jid={ANA} />
      </MemoryRouter>,
    )
    await screen.findByText('newest two')
    fireEvent.click(screen.getByRole('button', { name: 'Chat menu' }))
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search in this chat' }), { target: { value: 'target' } })
    await screen.findByText('the target')
    expect(screen.getByTestId('where').textContent).toBe(`/chat/${encodeURIComponent(ANA)}?around=O2`)
    await waitFor(() => expect(flashed()).toBe('O2'))
  })

  it('shows "No results" and leaves the chat alone when nothing matches', async () => {
    vi.spyOn(api, 'searchMessages').mockResolvedValue({ hits: [], next: null })
    render(
      <MemoryRouter>
        <Chat jid={ANA} />
      </MemoryRouter>,
    )
    await screen.findByText('newest two')
    fireEvent.click(screen.getByRole('button', { name: 'Chat menu' }))
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search in this chat' }), { target: { value: 'zzz' } })
    await screen.findByText('No results')
    expect(flashed()).toBeUndefined()
    await act(async () => {})
  })
})
