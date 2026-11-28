import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { MessageView, StarredItem } from '../api-types'
import { Starred } from './Starred'

const item = (id: string, text: string, chat = 'Ana Kovačević', jid = '385910000001@s.whatsapp.net'): StarredItem => ({
  chat_name: chat,
  chat_jid: jid,
  message: {
    id,
    chat_jid: jid,
    sender_jid: jid,
    sender_name: 'Ana',
    from_me: false,
    timestamp_iso: '2026-11-20T10:00:00+00:00',
    text,
    media: null,
    starred: true,
  } as unknown as MessageView,
})

function renderStarred() {
  return render(
    <MemoryRouter>
      <Starred />
    </MemoryRouter>,
  )
}

let fetchStarred: ReturnType<typeof vi.spyOn>
beforeEach(() => {
  fetchStarred = vi.spyOn(api, 'fetchStarred')
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('Starred screen', () => {
  it('lists starred messages in the order the server gave, each linking to the message in its chat', async () => {
    fetchStarred.mockResolvedValue({ items: [item('A2', 'second'), item('A1', 'first', 'Obitelj', '120363000000000002@g.us')], next: null })
    renderStarred()
    await screen.findByText(/second/)
    const rows = screen.getAllByRole('link').filter((l) => l.getAttribute('href')?.startsWith('/chat/'))
    expect(rows.map((r) => r.textContent)).toEqual([expect.stringContaining('second'), expect.stringContaining('first')])
    expect(rows[0].getAttribute('href')).toBe('/chat/385910000001%40s.whatsapp.net?around=A2')
    expect(screen.getByText('Obitelj')).toBeTruthy()
  })

  it('says so when nothing is starred', async () => {
    fetchStarred.mockResolvedValue({ items: [], next: null })
    renderStarred()
    expect(await screen.findByText('No starred messages yet.')).toBeTruthy()
  })

  it('loads the next page with the cursor', async () => {
    fetchStarred.mockResolvedValueOnce({ items: [item('A2', 'second')], next: 'CURSOR' })
    renderStarred()
    await screen.findByText(/second/)
    fetchStarred.mockResolvedValueOnce({ items: [item('A1', 'first')], next: null })
    fireEvent.click(screen.getByRole('button', { name: 'Show more' }))
    await screen.findByText(/first/)
    expect(fetchStarred).toHaveBeenLastCalledWith('CURSOR')
    expect(screen.queryByRole('button', { name: 'Show more' })).toBeNull()
  })

  it('offers to try again when loading fails', async () => {
    fetchStarred.mockRejectedValueOnce(new Error('x'))
    renderStarred()
    expect((await screen.findByRole('alert')).textContent).toContain('Could not load')
    fetchStarred.mockResolvedValueOnce({ items: [item('A1', 'first')], next: null })
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(screen.getByText(/first/)).toBeTruthy())
  })
})
