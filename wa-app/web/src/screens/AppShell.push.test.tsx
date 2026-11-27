import { cleanup, render, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import * as live from '../live'
import * as push from '../push'
import { AppShell } from './AppShell'

describe('AppShell push resync', () => {
  beforeEach(() => {
    push.resetResync()
    vi.spyOn(live, 'subscribeLive').mockImplementation(() => () => {})
    vi.spyOn(api, 'apiFetch').mockResolvedValue({ chats: [], items: [] })
    vi.stubGlobal('Notification', { permission: 'granted' })
    const sub = { endpoint: 'https://web.push.apple.com/x', toJSON: () => ({ endpoint: 'https://web.push.apple.com/x', keys: { p256dh: 'p', auth: 'a' } }) }
    Object.defineProperty(navigator, 'serviceWorker', {
      value: { ready: Promise.resolve({ pushManager: { getSubscription: vi.fn().mockResolvedValue(sub) } }) },
      configurable: true,
    })
    ;(window as unknown as Record<string, unknown>).PushManager = class {}
  })
  afterEach(() => {
    cleanup()
    delete (window as unknown as Record<string, unknown>).PushManager
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('re-registers the subscription after every login, not only on the first page load', async () => {
    const posts = () => vi.mocked(api.apiFetch).mock.calls.filter(([p]) => p === '/api/push/subscribe').length
    const shell = () => (
      <MemoryRouter>
        <AppShell onLogout={() => {}} />
      </MemoryRouter>
    )
    const first = render(shell())
    await waitFor(() => expect(posts()).toBe(1))
    first.unmount() // logout
    render(shell()) // login again, same page load
    await waitFor(() => expect(posts()).toBe(2))
  })
})
