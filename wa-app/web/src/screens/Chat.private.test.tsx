import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { ChatDetail, MessageView, MessagesResponse } from '../api-types'
import * as live from '../live'
import { resetOutbox } from '../outbox'
import { Chat } from './Chat'

const GROUP = '120363000000000014@g.us'
const NIKOLA = '385910000006@s.whatsapp.net'
const message = (extra: Partial<MessageView> = {}): MessageView => ({
  id: 'E01',
  chat_jid: GROUP,
  sender_jid: NIKOLA,
  sender_name: 'Nikola',
  from_me: false,
  timestamp_iso: '2026-11-23T10:00:00+00:00',
  text: 'Tko vozi u subotu?',
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
  ...extra,
})
const chatOf = (jid: string, name: string, kind: string) =>
  ({
    chat: { jid, name, kind, unread: 0, marked_unread: false, avatar_url: null },
    members_line: null,
  }) as unknown as ChatDetail

function Where() {
  const l = useLocation()
  return <p data-testid="where">{l.pathname + l.search}</p>
}

describe('Reply privately', () => {
  let send: ReturnType<typeof vi.spyOn>
  beforeEach(() => {
    resetOutbox()
    vi.spyOn(live, 'subscribeLive').mockImplementation(() => () => {})
    send = vi.spyOn(api, 'sendMessage').mockResolvedValue({ ok: true, message_id: 'S1' })
  })
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('the menu of someone else’s group message opens their chat with the group in the URL', async () => {
    vi.spyOn(api, 'apiFetch').mockImplementation(async (path: string) =>
      path.endsWith('/messages')
        ? ({ messages: [message(), message({ id: 'E02', from_me: true, text: 'Evo me' })], has_more: false } satisfies MessagesResponse)
        : chatOf(GROUP, 'Ekipa za vikend', 'group'),
    )
    render(
      <MemoryRouter initialEntries={[`/chat/${encodeURIComponent(GROUP)}`]}>
        <Where />
        <Chat jid={GROUP} />
      </MemoryRouter>,
    )
    const text = await screen.findByText('Tko vozi u subotu?')
    fireEvent.contextMenu(text)
    // My own message has no such item.
    fireEvent.contextMenu(screen.getByText('Evo me'))
    expect(screen.queryByText('Reply privately')).toBeNull()
    fireEvent.contextMenu(text)
    fireEvent.click(await screen.findByText('Reply privately'))
    await waitFor(() =>
      expect(screen.getByTestId('where').textContent).toBe(
        `/chat/${encodeURIComponent(NIKOLA)}?reply=E01&from=${encodeURIComponent(GROUP)}`,
      ),
    )
  })

  it('a direct chat has no Reply privately item', async () => {
    vi.spyOn(api, 'apiFetch').mockImplementation(async (path: string) =>
      path.endsWith('/messages')
        ? ({ messages: [message({ chat_jid: NIKOLA })], has_more: false } satisfies MessagesResponse)
        : chatOf(NIKOLA, 'Nikola', 'private'),
    )
    render(
      <MemoryRouter>
        <Chat jid={NIKOLA} />
      </MemoryRouter>,
    )
    fireEvent.contextMenu(await screen.findByText('Tko vozi u subotu?'))
    await screen.findByText('Reply')
    expect(screen.queryByText('Reply privately')).toBeNull()
  })

  it('opens the sender’s empty chat with a quote bar reading "sender · group" and the send carries reply_chat', async () => {
    vi.spyOn(api, 'apiFetch').mockImplementation(async (path: string) => {
      if (path === `/api/chats/${encodeURIComponent(NIKOLA)}/messages`) return { messages: [], has_more: false }
      if (path === `/api/chats/${encodeURIComponent(NIKOLA)}`) return chatOf(NIKOLA, 'Nikola', 'private')
      if (path === `/api/chats/${encodeURIComponent(GROUP)}/messages/E01`) return { message: message() }
      if (path === `/api/chats/${encodeURIComponent(GROUP)}`) return chatOf(GROUP, 'Ekipa za vikend', 'group')
      throw new Error(`unexpected ${path}`)
    })
    render(
      <MemoryRouter
        initialEntries={[`/chat/${encodeURIComponent(NIKOLA)}?reply=E01&from=${encodeURIComponent(GROUP)}`]}
      >
        <Where />
        <Routes>
          <Route path="/chat/:jid" element={<Chat jid={NIKOLA} />} />
        </Routes>
      </MemoryRouter>,
    )
    expect(await screen.findByText('Nikola · Ekipa za vikend')).toBeTruthy()
    expect(screen.getByText('Tko vozi u subotu?')).toBeTruthy()
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe(`/chat/${encodeURIComponent(NIKOLA)}`))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Ja vozim' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await waitFor(() => expect(send).toHaveBeenCalledTimes(1))
    const args = send.mock.calls[0]
    expect(args[0]).toBe(NIKOLA)
    expect(args[1]).toBe('Ja vozim')
    expect(args[2]).toBe('E01')
    expect(args[5]).toBe(GROUP)
  })

  it('cancelling the quote sends a plain message with no reply_chat', async () => {
    vi.spyOn(api, 'apiFetch').mockImplementation(async (path: string) => {
      if (path.endsWith('/messages')) return { messages: [], has_more: false }
      if (path.includes('/messages/E01')) return { message: message() }
      return path.includes('120363') ? chatOf(GROUP, 'Ekipa za vikend', 'group') : chatOf(NIKOLA, 'Nikola', 'private')
    })
    render(
      <MemoryRouter initialEntries={[`/chat/x?reply=E01&from=${encodeURIComponent(GROUP)}`]}>
        <Chat jid={NIKOLA} />
      </MemoryRouter>,
    )
    await screen.findByText('Nikola · Ekipa za vikend')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel reply' }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Hej' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await waitFor(() => expect(send).toHaveBeenCalledTimes(1))
    expect(send.mock.calls[0][2]).toBeNull()
    expect(send.mock.calls[0][5]).toBeUndefined()
  })

  it('a person with no direct chat yet says so instead of a blank screen', async () => {
    vi.spyOn(api, 'apiFetch').mockRejectedValue(new api.ApiError(404, 'Chat not found'))
    render(
      <MemoryRouter initialEntries={[`/chat/888%40lid?reply=E01&from=${encodeURIComponent(GROUP)}`]}>
        <Chat jid="888@lid" />
      </MemoryRouter>,
    )
    expect(await screen.findByText("Can't reply privately to this person yet.")).toBeTruthy()
  })
})

describe('Open chat report', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('reports the canonical chat jid, not the LID in the route', async () => {
    const LID = '99887766@lid'
    vi.spyOn(live, 'subscribeLive').mockImplementation(() => () => {})
    const open = vi.spyOn(live, 'setOpenChat')
    vi.spyOn(api, 'apiFetch').mockImplementation(async (path: string) =>
      path.endsWith('/messages') ? { messages: [], has_more: false } : chatOf(NIKOLA, 'Nikola', 'private'),
    )
    render(
      <MemoryRouter>
        <Chat jid={LID} />
      </MemoryRouter>,
    )
    await waitFor(() => expect(open).toHaveBeenLastCalledWith(NIKOLA))
  })
})
