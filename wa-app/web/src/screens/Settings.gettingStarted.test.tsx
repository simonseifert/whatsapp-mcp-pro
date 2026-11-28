import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { Settings } from './Settings'

const DISMISSED = 'wa-app.getting-started.dismissed'
const chat = (jid: string, thin: boolean) => ({
  jid,
  name: jid,
  kind: 'direct',
  messages: 10,
  own_texts: thin ? 1 : 100,
  oldest: null,
  last_active: null,
  thin,
  backfill_jid: null,
})
const backend = { id: 1, kind: 'openai', label: 'Ollama', base_url: null, model: 'llama3', has_key: false, local: true }

type World = { push: boolean; thin: number; backends: boolean }
function setup({ push, thin, backends }: World) {
  vi.spyOn(api, 'apiFetch').mockImplementation(((path: string) => {
    if (path === '/api/push/key') return push ? Promise.resolve({ key: 'k' }) : Promise.reject(new api.ApiError(503, 'Push not configured'))
    return Promise.resolve({ version: '0.0.0' })
  }) as never)
  vi.spyOn(api, 'fetchBackends').mockResolvedValue({ backends: backends ? [backend] : [], default_id: backends ? 1 : null } as never)
  vi.spyOn(api, 'fetchDraftContext').mockResolvedValue({ context_messages: 30, own_examples: 30, about_me: '' })
  vi.spyOn(api, 'fetchCoverage').mockResolvedValue({
    totals: { messages: 0, chats: 0, oldest: null },
    chats: [...Array.from({ length: thin }, (_, i) => chat(`t${i}@x`, true)), chat('ok@x', false)],
    thin_threshold: 30,
  })
  vi.spyOn(api, 'fetchHistoryStatus').mockResolvedValue({ running: null, queued: [], done: [] })
  render(<Settings onLogout={() => {}} />)
}

beforeEach(() => {
  localStorage.clear()
  vi.stubGlobal('Notification', { permission: 'default' })
  ;(window as unknown as Record<string, unknown>).PushManager = class {}
  Object.defineProperty(navigator, 'serviceWorker', { value: { ready: new Promise(() => {}) }, configurable: true })
})
afterEach(() => {
  cleanup()
  delete (window as unknown as Record<string, unknown>).PushManager
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const card = () => screen.queryByRole('region', { name: 'Getting started' })

describe('Getting started card', () => {
  it('shows all three rows for a new install', async () => {
    setup({ push: false, thin: 2, backends: false })
    await screen.findByText('Turn on notifications')
    expect(screen.getByText('Load older messages')).toBeTruthy()
    expect(screen.getByText('Set up AI drafts')).toBeTruthy()
    const c = within(card() as HTMLElement)
    expect(c.getByText(/WA_APP_PUSH_SUBJECT=mailto:you@example\.com/)).toBeTruthy()
    expect(c.getByText(/wactl setup/)).toBeTruthy()
    expect(c.getByText(/wactl restart app/)).toBeTruthy()
    expect(c.getByText(/2 chats/)).toBeTruthy()
  })

  it('hides the notifications row when push works', async () => {
    setup({ push: true, thin: 1, backends: false })
    await screen.findByText('Load older messages')
    expect(screen.queryByText('Turn on notifications')).toBeNull()
  })

  it('hides the history row when no chat is thin', async () => {
    setup({ push: false, thin: 0, backends: false })
    await screen.findByText('Turn on notifications')
    expect(screen.queryByText('Load older messages')).toBeNull()
  })

  it('hides the AI row once a backend exists', async () => {
    setup({ push: false, thin: 1, backends: true })
    await screen.findByText('Turn on notifications')
    expect(screen.queryByText('Set up AI drafts')).toBeNull()
  })

  it('is gone when nothing is open', async () => {
    setup({ push: true, thin: 0, backends: true })
    await screen.findByText('Ollama')
    await waitFor(() => expect(api.fetchCoverage).toHaveBeenCalled())
    expect(card()).toBeNull()
  })

  it('runs the thin-chat fill from its row', async () => {
    const fill = vi.spyOn(api, 'fillHistory').mockResolvedValue({ added: 2, status: { running: null, queued: [], done: [] } })
    setup({ push: true, thin: 2, backends: true })
    await screen.findByText('Load older messages')
    fireEvent.click(within(card() as HTMLElement).getByRole('button', { name: 'Fill thin chats' }))
    await waitFor(() => expect(fill).toHaveBeenCalledWith('thin', '3y'))
  })

  it('dismiss hides the card and remembers it', async () => {
    setup({ push: false, thin: 1, backends: false })
    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss getting started' }))
    expect(card()).toBeNull()
    expect(localStorage.getItem(DISMISSED)).toBe('1')
  })

  it('stays hidden when dismissed earlier', async () => {
    localStorage.setItem(DISMISSED, '1')
    setup({ push: false, thin: 1, backends: false })
    await waitFor(() => expect(api.fetchBackends).toHaveBeenCalled())
    expect(card()).toBeNull()
  })

  it('shows the explanation instead of a dead notifications switch', async () => {
    setup({ push: false, thin: 0, backends: true })
    await waitFor(() => expect(screen.queryByRole('switch', { name: 'Notifications' })).toBeNull())
    expect(screen.getAllByText(/WA_APP_PUSH_SUBJECT/).length).toBeGreaterThan(0)
  })
})
