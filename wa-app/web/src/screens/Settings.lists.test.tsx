import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { Settings } from './Settings'

beforeEach(() => {
  vi.spyOn(api, 'apiFetch').mockResolvedValue({ version: '0.0.0' })
  vi.spyOn(api, 'fetchBackends').mockResolvedValue({ backends: [], default_id: null })
  vi.spyOn(api, 'fetchDraftContext').mockResolvedValue({ context_messages: 30, own_examples: 30, about_me: '' })
  vi.spyOn(api, 'fetchCoverage').mockResolvedValue({ totals: { messages: 0, chats: 0, oldest: null }, chats: [], thin_threshold: 30 })
  vi.spyOn(api, 'fetchHistoryStatus').mockResolvedValue({ running: null, queued: [], done: [] })
  vi.spyOn(api, 'fetchLists').mockResolvedValue([{ id: 1, name: 'Work', position: 0, count: 0 }])
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('Settings lists entry', () => {
  it('opens the Lists screen and comes back', async () => {
    render(<Settings onLogout={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Lists' }))
    expect(await screen.findByText('Work')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
    expect(screen.getByRole('heading', { name: 'Settings' })).toBeTruthy()
  })
})
