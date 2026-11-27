import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { ChatDetail } from '../api-types'
import * as live from '../live'
import { resetOutbox } from '../outbox'
import { Chat } from './Chat'

const ANA = '385910000001@s.whatsapp.net'
const detail = {
  chat: {
    jid: ANA,
    name: 'Ana',
    kind: 'private',
    unread: 0,
    marked_unread: false,
    pinned: false,
    muted: false,
    archived: false,
    avatar_url: null,
  },
  members_line: null,
} as unknown as ChatDetail

describe('Chat: header menu actions', () => {
  beforeEach(() => {
    resetOutbox()
    vi.spyOn(live, 'subscribeLive').mockImplementation(() => () => undefined)
    vi.spyOn(api, 'apiFetch').mockImplementation(async (path: string) => {
      if (path.includes('/messages')) return { messages: [], has_more: false }
      return detail
    })
    vi.spyOn(api, 'setChatState').mockImplementation(async (_jid, patch) => ({
      ok: true,
      chat: { ...detail.chat, muted: patch.mute !== undefined && patch.mute !== false },
    }))
  })
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('mutes from the header menu and then offers Unmute', async () => {
    render(
      <MemoryRouter>
        <Chat jid={ANA} />
      </MemoryRouter>,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Chat menu' }))
    fireEvent.click(screen.getByRole('button', { name: 'Mute notifications' }))
    fireEvent.click(screen.getByRole('button', { name: '1 week' }))
    await waitFor(() => expect(api.setChatState).toHaveBeenCalledWith(ANA, { mute: '1w' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Chat menu' }))
    expect(await screen.findByRole('button', { name: 'Unmute notifications' })).toBeTruthy()
  })
})
