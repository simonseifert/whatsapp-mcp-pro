import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { ChatSummary } from '../api-types'
import { ChatRow } from './ChatRow'

const JID = '385910000001@s.whatsapp.net'
const chat = (extra: Partial<ChatSummary> = {}): ChatSummary => ({
  jid: JID,
  name: 'Ana',
  kind: 'private',
  avatar_url: '',
  last_message: {
    id: 'M1',
    preview: 'hi',
    media_type: null,
    timestamp_iso: new Date().toISOString(),
    from_me: false,
    sender_name: 'Ana',
    ticks: null,
    deleted: false,
  },
  unread: 0,
  marked_unread: false,
  pinned: false,
  muted: false,
  archived: false,
  community_name: null,
  ...extra,
})
const mount = (c: ChatSummary) =>
  render(
    <MemoryRouter>
      <ul>
        <ChatRow chat={c} />
      </ul>
    </MemoryRouter>,
  )

beforeEach(() => {
  vi.spyOn(api, 'setChatState').mockImplementation(async (_jid, patch) => ({
    ok: true,
    chat: chat({ pinned: patch.pin === true, marked_unread: patch.unread === true }),
  }))
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('ChatRow actions', () => {
  it('opens the chat menu on right click and pins', async () => {
    mount(chat())
    fireEvent.contextMenu(screen.getByRole('link'))
    fireEvent.click(screen.getByRole('button', { name: 'Pin chat' }))
    await waitFor(() => expect(api.setChatState).toHaveBeenCalledWith(JID, { pin: true }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByLabelText('Pinned')).toBeTruthy() // the optimistic state shows before the list refreshes
  })

  it('opens the menu on a long press and not on a short tap', () => {
    vi.useFakeTimers()
    mount(chat())
    const link = screen.getByRole('link')
    fireEvent.touchStart(link, { touches: [{ clientX: 1, clientY: 1 }] })
    act(() => void vi.advanceTimersByTime(200))
    fireEvent.touchEnd(link)
    act(() => void vi.advanceTimersByTime(600))
    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.touchStart(link, { touches: [{ clientX: 1, clientY: 1 }] })
    act(() => void vi.advanceTimersByTime(600))
    expect(screen.getByRole('dialog', { name: 'Chat actions' })).toBeTruthy()
  })

  it('shows the dot once marked as unread, with no unread messages', async () => {
    mount(chat())
    fireEvent.contextMenu(screen.getByRole('link'))
    fireEvent.click(screen.getByRole('button', { name: 'Mark as unread' }))
    expect(await screen.findByRole('img', { name: 'Marked unread' })).toBeTruthy()
  })

  it('says so when the change failed and leaves the row as it was', async () => {
    vi.spyOn(api, 'setChatState').mockResolvedValue({ ok: false, error: 'You can pin up to 3 chats' })
    mount(chat())
    fireEvent.contextMenu(screen.getByRole('link'))
    fireEvent.click(screen.getByRole('button', { name: 'Pin chat' }))
    expect((await screen.findByRole('alert')).textContent).toContain('You can pin up to 3 chats')
    expect(screen.queryByLabelText('Pinned')).toBeNull()
  })
})
