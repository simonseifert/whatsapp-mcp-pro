import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { ChatAi } from '../api-types'
import { ChatAiSheet, isCloud } from './ChatAiSheet'

const JID = 'a@s.whatsapp.net'
const ai: ChatAi = { chat_jid: JID, enabled: false, backend_id: null, local_only: false, note: null, rules: null }
const backends = {
  default_id: 1,
  backends: [
    { id: 1, kind: 'openai', label: 'Ollama', base_url: 'http://127.0.0.1:11434/v1', model: 'q', has_key: false },
    { id: 2, kind: 'anthropic', label: 'Claude API', base_url: null, model: 'c', has_key: true },
    { id: 3, kind: 'openai', label: 'Remote', base_url: 'https://my-ollama.example.com/v1', model: 'q', has_key: true },
  ],
}

beforeEach(() => {
  vi.spyOn(api, 'fetchChatAi').mockResolvedValue(ai)
  vi.spyOn(api, 'fetchBackends').mockResolvedValue(backends as never)
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('isCloud', () => {
  it('treats non-openai and non-private hosts as cloud', () => {
    expect(isCloud(backends.backends[0] as never)).toBe(false)
    expect(isCloud({ ...backends.backends[0], base_url: 'http://192.168.1.5:11434' } as never)).toBe(false)
    expect(isCloud({ ...backends.backends[0], base_url: 'http://172.20.0.2' } as never)).toBe(false)
    expect(isCloud({ ...backends.backends[0], base_url: 'http://172.32.0.2' } as never)).toBe(true)
    expect(isCloud(backends.backends[1] as never)).toBe(true)
    expect(isCloud(backends.backends[2] as never)).toBe(true)
  })
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
  })

  it('says so and blocks Save when Local only has no local backend', async () => {
    vi.spyOn(api, 'fetchBackends').mockResolvedValue({ default_id: 2, backends: [backends.backends[1]] } as never)
    render(<ChatAiSheet jid={JID} onClose={() => {}} />)
    fireEvent.click(await screen.findByLabelText('Local only'))
    expect(screen.getByText('Add a local AI in Settings first')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByRole('dialog').getAttribute('aria-modal')).toBe('true')
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
})
