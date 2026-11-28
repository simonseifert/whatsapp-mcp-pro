import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { ListSheet } from './ListSheet'

const JID = '385910000001@s.whatsapp.net'

beforeEach(() => {
  vi.spyOn(api, 'fetchLists').mockResolvedValue([
    { id: 1, name: 'Work', position: 0, count: 1, member: true },
    { id: 2, name: 'Family', position: 1, count: 0, member: false },
  ])
  vi.spyOn(api, 'changeListChats').mockResolvedValue(undefined)
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('ListSheet', () => {
  it('shows which lists the chat is in', async () => {
    render(<ListSheet jid={JID} onClose={() => {}} />)
    const work = (await screen.findByRole('switch', { name: 'Work' })) as HTMLInputElement
    expect(work.checked).toBe(true)
    expect((screen.getByRole('switch', { name: 'Family' }) as HTMLInputElement).checked).toBe(false)
  })

  it('adds and removes the chat as the boxes change', async () => {
    render(<ListSheet jid={JID} onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('switch', { name: 'Family' }))
    await waitFor(() => expect(api.changeListChats).toHaveBeenCalledWith(2, { add: [JID], remove: [] }))
    fireEvent.click(screen.getByRole('switch', { name: 'Work' }))
    await waitFor(() => expect(api.changeListChats).toHaveBeenCalledWith(1, { add: [], remove: [JID] }))
  })

  it('points at Settings when there are no lists yet', async () => {
    vi.spyOn(api, 'fetchLists').mockResolvedValue([])
    render(<ListSheet jid={JID} onClose={() => {}} />)
    expect(await screen.findByText('No lists yet. Create one in Settings.')).toBeTruthy()
  })
})
