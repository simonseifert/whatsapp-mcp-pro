import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { ChatDetail, DraftView, LiveEvent, MessagesResponse } from '../api-types'
import * as live from '../live'
import { resetOutbox } from '../outbox'
import { Chat } from './Chat'

const JID = 'g@g.us'
const detail = {
  chat: { jid: JID, name: 'Planinari', kind: 'group', unread: 0, marked_unread: false, avatar_url: null },
  members_line: null,
} as unknown as ChatDetail
const page: MessagesResponse = {
  has_more: false,
  messages: [
    {
      id: 'M1',
      chat_jid: JID,
      sender_jid: 'n@s.whatsapp.net',
      sender_name: 'Nikola',
      from_me: false,
      timestamp_iso: '2026-11-23T10:00:00+00:00',
      text: 'Gdje si?',
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
    },
  ],
}
const draft: DraftView = {
  id: 11,
  chat_jid: JID,
  trigger_id: 'M1',
  status: 'ready',
  alternatives: ['Stižem za deset minuta'],
  error: null,
  quote: { id: 'M1', sender_name: 'Nikola', preview: 'Gdje si?' },
  created_at_iso: '2026-11-23T10:01:00+00:00',
}

describe('Chat with a draft', () => {
  let emit: (e: LiveEvent) => void
  let send: ReturnType<typeof vi.spyOn>
  beforeEach(() => {
    resetOutbox()
    vi.spyOn(live, 'subscribeLive').mockImplementation((l) => {
      emit = l
      return () => {}
    })
    vi.spyOn(api, 'apiFetch').mockImplementation(async (path: string) => (path.endsWith('/messages') ? page : detail))
    vi.spyOn(api, 'fetchDraft').mockResolvedValue(draft)
    send = vi.spyOn(api, 'sendMessage').mockResolvedValue({ ok: true, message_id: 'S1' })
  })
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  const open = async () => {
    render(
      <MemoryRouter>
        <Chat jid={JID} />
      </MemoryRouter>,
    )
    await screen.findByText('Draft · replying to Nikola')
  }

  it('Use fills the composer, replies to the trigger in the group and the send carries draft_id', async () => {
    await open()
    fireEvent.click(screen.getByRole('button', { name: /Stižem za deset minuta/ }))
    const box = screen.getByLabelText('Message') as HTMLTextAreaElement
    expect(box.value).toBe('Stižem za deset minuta')
    expect(screen.getByRole('button', { name: 'Cancel reply' })).toBeTruthy()
    expect(send).not.toHaveBeenCalled() // a draft never sends itself
    expect(screen.queryByText('Draft · replying to Nikola')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(send).toHaveBeenCalled())
    expect(send).toHaveBeenCalledWith(JID, 'Stižem za deset minuta', 'M1', expect.any(String), 11)
  })

  it('a send after the box was cleared and retyped carries no draft_id', async () => {
    await open()
    fireEvent.click(screen.getByRole('button', { name: /Stižem/ }))
    const box = screen.getByLabelText('Message') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: '' } })
    fireEvent.change(box, { target: { value: 'nešto drugo' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(send).toHaveBeenCalled())
    expect(send).toHaveBeenCalledWith(JID, 'nešto drugo', 'M1', expect.any(String), undefined)
  })

  it('dismissing the card forgets the used draft', async () => {
    vi.spyOn(api, 'dismissDraft').mockResolvedValue(null)
    await open()
    fireEvent.click(screen.getByRole('button', { name: /Stižem/ }))
    act(() => emit({ name: 'draft', data: { chat_jid: JID, draft: { ...draft, id: 12 } } }))
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss draft' }))
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(send).toHaveBeenCalled())
    expect(send.mock.calls[0][4]).toBeUndefined()
  })

  it('the chat menu closes on Escape and gives focus back to its button', async () => {
    await open()
    const opener = screen.getByRole('button', { name: 'Chat menu' })
    opener.focus()
    fireEvent.click(opener)
    expect(screen.getByRole('dialog', { name: 'Chat actions' }).getAttribute('aria-modal')).toBe('true')
    expect(document.activeElement?.textContent).toContain('AI drafts')
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'Chat actions' })).toBeNull()
    expect(document.activeElement).toBe(opener)
  })

  it('asks before replacing text that is already typed', async () => {
    await open()
    const box = screen.getByLabelText('Message') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'moj tekst' } })
    fireEvent.click(screen.getByRole('button', { name: /Stižem/ }))
    expect(screen.getByText('Replace your text?')).toBeTruthy()
    expect(box.value).toBe('moj tekst')
    fireEvent.click(screen.getByRole('button', { name: 'Replace' }))
    expect(box.value).toBe('Stižem za deset minuta')
  })

  it('dismiss calls DELETE and a null draft event removes the card', async () => {
    const del = vi.spyOn(api, 'dismissDraft').mockResolvedValue(null)
    await open()
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss draft' }))
    expect(del).toHaveBeenCalledWith(11)
    expect(screen.queryByText('Draft · replying to Nikola')).toBeNull()
    act(() => emit({ name: 'draft', data: { chat_jid: JID, draft } }))
    expect(screen.getByText('Draft · replying to Nikola')).toBeTruthy()
    act(() => emit({ name: 'draft', data: { chat_jid: JID, draft: null } }))
    expect(screen.queryByText('Draft · replying to Nikola')).toBeNull()
  })

  it('the Draft button requests a one-off draft and Try again repeats the same trigger', async () => {
    vi.spyOn(api, 'fetchDraft').mockResolvedValue({ ...draft, status: 'failed', alternatives: [], error: 'This chat is local only' })
    const post = vi.spyOn(api, 'requestDraft').mockResolvedValue(draft)
    await open()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(post).toHaveBeenCalledWith(JID, 'M1')
    fireEvent.click(screen.getByRole('button', { name: 'Draft a reply' }))
    expect(post).toHaveBeenLastCalledWith(JID, undefined)
  })
})
