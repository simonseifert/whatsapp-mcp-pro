import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { READY_TIMEOUT_MS } from '../push'
import { Settings } from './Settings'

beforeEach(() => {
  vi.useFakeTimers()
  vi.spyOn(api, 'apiFetch').mockResolvedValue({ version: '0.0.0' })
  vi.spyOn(api, 'fetchBackends').mockResolvedValue({ backends: [], default_id: null })
  vi.spyOn(api, 'fetchDraftContext').mockResolvedValue({ context_messages: 30, own_examples: 30, about_me: '' })
  vi.spyOn(api, 'fetchCoverage').mockResolvedValue({ totals: { messages: 0, chats: 0, oldest: null }, chats: [], thin_threshold: 30 })
  vi.spyOn(api, 'fetchHistoryStatus').mockResolvedValue({ running: null, queued: [], done: [] })
  vi.stubGlobal('Notification', { permission: 'default' })
  ;(window as unknown as Record<string, unknown>).PushManager = class {}
  Object.defineProperty(navigator, 'serviceWorker', { value: { ready: new Promise(() => {}) }, configurable: true })
})
afterEach(() => {
  cleanup()
  delete (window as unknown as Record<string, unknown>).PushManager
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('Settings notifications', () => {
  it('shows the switch off with a note when the service worker is not ready after 5 s', async () => {
    render(<Settings onLogout={() => {}} />)
    const toggle = screen.getByRole('switch', { name: 'Notifications' }) as HTMLInputElement
    expect(toggle.disabled).toBe(true)
    await act(() => vi.advanceTimersByTimeAsync(READY_TIMEOUT_MS))
    expect(toggle.disabled).toBe(false)
    expect(toggle.checked).toBe(false)
    expect(screen.getByText("Notifications aren't ready yet. Close and reopen the app, then try again.")).toBeTruthy()
  })
})
