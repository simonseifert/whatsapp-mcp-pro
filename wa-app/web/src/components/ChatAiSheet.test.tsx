import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { ChatAi } from '../api-types'
import { ChatAiSheet } from './ChatAiSheet'

const JID = 'a@s.whatsapp.net'
const ai: ChatAi = { chat_jid: JID, enabled: false, backend_id: null, local_only: false, note: null, rules: null }
const backends = {
  default_id: 1,
  backends: [
    { id: 1, kind: 'openai', label: 'Ollama', base_url: 'http://127.0.0.1:11434/v1', model: 'q', has_key: false, local: true },
    { id: 2, kind: 'anthropic', label: 'Claude API', base_url: null, model: 'c', has_key: true, local: false },
    { id: 3, kind: 'openai', label: 'Remote', base_url: 'https://my-ollama.example.com/v1', model: 'q', has_key: true, local: false },
    { id: 4, kind: 'openai', label: 'Home', base_url: 'http://ollama.home:11434/v1', model: 'q', has_key: false, local: true },
  ],
}

beforeEach(() => {
  vi.spyOn(api, 'fetchChatAi').mockResolvedValue(ai)
  vi.spyOn(api, 'fetchBackends').mockResolvedValue(backends as never)
  vi.spyOn(api, 'fetchHistoryStatus').mockResolvedValue({ running: null, queued: [], done: [] })
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('ChatAiSheet', () => {
  it('disables cloud backends with the reason when Local only is on', async () => {
    render(<ChatAiSheet jid={JID} onClose={() => {}} />)
    await screen.findByLabelText('Drafts in this chat')
    const claude = screen.getByRole('option', { name: /Claude API/ }) as HTMLOptionElement
    expect(claude.disabled).toBe(false)
    fireEvent.click(screen.getByLabelText('Local only'))
    expect((screen.getByRole('option', { name: /Claude API/ }) as HTMLOptionElement).disabled).toBe(true)
    expect(screen.getByRole('option', { name: /Claude API · Not local/ })).toBeTruthy()
    expect((screen.getByRole('option', { name: /Remote/ }) as HTMLOptionElement).disabled).toBe(true)
    expect((screen.getByRole('option', { name: /^Ollama$/ }) as HTMLOptionElement).disabled).toBe(false)
    // a private DNS name the server calls local can be chosen, whatever its spelling
    expect((screen.getByRole('option', { name: /^Home$/ }) as HTMLOptionElement).disabled).toBe(false)
  })

  it('says so and blocks Save when Local only has no local backend', async () => {
    vi.spyOn(api, 'fetchBackends').mockResolvedValue({ default_id: 2, backends: [backends.backends[1]] } as never)
    render(<ChatAiSheet jid={JID} onClose={() => {}} />)
    fireEvent.click(await screen.findByLabelText('Local only'))
    expect(screen.getByText('Add a local AI in Settings first')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByRole('dialog').getAttribute('aria-modal')).toBe('true')
  })

  it('exposes both toggles as labelled switches', async () => {
    render(<ChatAiSheet jid={JID} onClose={() => {}} />)
    const drafts = await screen.findByRole('switch', { name: 'Drafts in this chat' })
    const local = screen.getByRole('switch', { name: 'Local only' })
    expect((drafts as HTMLInputElement).checked).toBe(false)
    fireEvent.click(local)
    expect((local as HTMLInputElement).checked).toBe(true)
  })

  it('shows the missing-local-AI notice as a warning with an icon', async () => {
    vi.spyOn(api, 'fetchBackends').mockResolvedValue({ default_id: 2, backends: [backends.backends[1]] } as never)
    render(<ChatAiSheet jid={JID} onClose={() => {}} />)
    fireEvent.click(await screen.findByLabelText('Local only'))
    const warning = screen.getByRole('alert')
    expect(warning.className).toContain('sheet-warning')
    expect(warning.querySelector('svg')).toBeTruthy()
  })

  it('saves the chosen settings', async () => {
    const save = vi.spyOn(api, 'saveChatAi').mockResolvedValue(ai)
    const onClose = vi.fn()
    render(<ChatAiSheet jid={JID} onClose={onClose} />)
    fireEvent.click(await screen.findByLabelText('Drafts in this chat'))
    fireEvent.change(screen.getByLabelText('Note'), { target: { value: 'Keep it short' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(save).toHaveBeenCalledWith(JID, {
      enabled: true,
      backend_id: null,
      local_only: false,
      note: 'Keep it short',
      rules: '',
    })
  })

  it('follows this chat from queued to running to done, then re-enables the button', async () => {
    vi.useFakeTimers()
    const status = vi.mocked(api.fetchHistoryStatus)
    vi.spyOn(api, 'loadChatHistory').mockResolvedValue({ added: 1, status: { running: null, queued: [JID], done: [] } })
    render(<ChatAiSheet jid={JID} onClose={() => {}} />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    const button = screen.getByRole('button', { name: 'Load older messages' }) as HTMLButtonElement
    await act(async () => {
      fireEvent.click(button)
    })
    expect(screen.getByText('Queued')).toBeTruthy()
    expect(button.disabled).toBe(true)
    status.mockResolvedValue({ running: { jid: JID, batches: 3, oldest: '2024-05-10T10:00:00Z' }, queued: [], done: [] })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })
    expect(screen.getByText('Loading older messages: 3 batches, back to May 2024')).toBeTruthy()
    status.mockResolvedValue({ running: null, queued: [], done: [{ jid: JID, result: 'more', detail: '', oldest: null }] })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })
    expect(screen.getByText('More available, run again')).toBeTruthy()
    expect(screen.queryByText('Queued')).toBeNull()
    expect(button.disabled).toBe(false)
    const calls = status.mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20000)
    })
    expect(status.mock.calls.length).toBe(calls)
  })

  it('does not say Queued when nothing was added and the chat already finished', async () => {
    vi.spyOn(api, 'loadChatHistory').mockResolvedValue({
      added: 0,
      status: { running: null, queued: [], done: [{ jid: JID, result: 'failed', detail: '', oldest: null }] },
    })
    render(<ChatAiSheet jid={JID} onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Load older messages' }))
    await waitFor(() => expect(screen.getByText('Failed')).toBeTruthy())
    expect(screen.queryByText('Queued')).toBeNull()
  })

  it('shows why older messages could not be queued', async () => {
    vi.spyOn(api, 'loadChatHistory').mockRejectedValue(new api.ApiError(404, 'Chat not found'))
    render(<ChatAiSheet jid={JID} onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Load older messages' }))
    await waitFor(() => expect(screen.getByText('Chat not found')).toBeTruthy())
  })
})
