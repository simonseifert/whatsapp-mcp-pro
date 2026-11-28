import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { ChatSummary } from '../api-types'
import { ForwardSheet } from './ForwardSheet'

const chat = (n: number, name: string): ChatSummary =>
  ({ jid: `3859100000${String(n).padStart(2, '0')}@s.whatsapp.net`, name, kind: 'private', avatar_url: '' }) as unknown as ChatSummary
const CHATS = [chat(1, 'Ana'), chat(2, 'Bruno'), chat(3, 'Cvita'), chat(4, 'Dario'), chat(5, 'Edo'), chat(6, 'Fran')]
const FROM = '120363000000000014@g.us'

let forward: ReturnType<typeof vi.spyOn>
beforeEach(() => {
  vi.spyOn(api, 'apiFetch').mockResolvedValue({ chats: CHATS, archived_count: 0, has_more: false })
  forward = vi.spyOn(api, 'forwardMessages')
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const pick = async (name: string) => fireEvent.click(await screen.findByRole('checkbox', { name }))

describe('ForwardSheet', () => {
  it('sends the chosen messages to the chosen chats and says where they went', async () => {
    forward.mockResolvedValue({ ok: true, results: [{ to: CHATS[0].jid, ok: true }, { to: CHATS[1].jid, ok: true }] })
    const onClose = vi.fn()
    render(<ForwardSheet fromChat={FROM} messageIds={['E1']} onClose={onClose} />)
    await pick('Ana')
    await pick('Bruno')
    fireEvent.click(screen.getByRole('button', { name: 'Forward to 2 chats' }))
    await waitFor(() => expect(forward).toHaveBeenCalledWith(FROM, ['E1'], [CHATS[0].jid, CHATS[1].jid], expect.any(String)))
    expect(await screen.findByText('Forwarded to 2 chats')).toBeTruthy()
  })

  it('allows at most five chats', async () => {
    render(<ForwardSheet fromChat={FROM} messageIds={['E1']} onClose={() => {}} />)
    for (const n of ['Ana', 'Bruno', 'Cvita', 'Dario', 'Edo']) await pick(n)
    expect((screen.getByRole('checkbox', { name: 'Fran' }) as HTMLInputElement).disabled).toBe(true)
    expect(screen.getByText('You can pick up to 5 chats.')).toBeTruthy()
  })

  it('shows each chat that failed with its reason, and Try again sends only those', async () => {
    forward.mockResolvedValueOnce({
      ok: true,
      results: [
        { to: CHATS[0].jid, ok: true },
        { to: CHATS[1].jid, ok: false, error: "Couldn't download the file to forward" },
      ],
    })
    render(<ForwardSheet fromChat={FROM} messageIds={['E1']} onClose={() => {}} />)
    await pick('Ana')
    await pick('Bruno')
    fireEvent.click(screen.getByRole('button', { name: 'Forward to 2 chats' }))
    expect(await screen.findByText("Bruno: Couldn't download the file to forward")).toBeTruthy()
    expect(screen.getByText('Ana: Forwarded')).toBeTruthy()
    forward.mockResolvedValueOnce({ ok: true, results: [{ to: CHATS[1].jid, ok: true }] })
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(forward).toHaveBeenLastCalledWith(FROM, ['E1'], [CHATS[1].jid], expect.any(String)))
  })

  it('a request that fails as a whole is shown and keeps the picks', async () => {
    forward.mockResolvedValue({ ok: false, error: "Not sent: server didn't answer" })
    render(<ForwardSheet fromChat={FROM} messageIds={['E1']} onClose={() => {}} />)
    await pick('Ana')
    fireEvent.click(screen.getByRole('button', { name: 'Forward to 1 chat' }))
    expect((await screen.findByRole('alert')).textContent).toContain("server didn't answer")
    expect((screen.getByRole('checkbox', { name: 'Ana' }) as HTMLInputElement).checked).toBe(true)
  })

  it('filters the chats by name', async () => {
    render(<ForwardSheet fromChat={FROM} messageIds={['E1']} onClose={() => {}} />)
    await screen.findByRole('checkbox', { name: 'Ana' })
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search chats' }), { target: { value: 'dar' } })
    expect(screen.queryByRole('checkbox', { name: 'Ana' })).toBeNull()
    expect(screen.getByRole('checkbox', { name: 'Dario' })).toBeTruthy()
  })
})
