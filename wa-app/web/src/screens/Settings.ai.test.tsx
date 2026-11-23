import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { Settings } from './Settings'

const list = {
  default_id: 1,
  backends: [
    { id: 1, kind: 'openai', label: 'Ollama', base_url: 'http://127.0.0.1:11434/v1', model: 'llama3', has_key: false },
    { id: 2, kind: 'anthropic', label: 'Claude API', base_url: null, model: 'claude-sonnet-5-5', has_key: true },
  ],
}

beforeEach(() => {
  vi.spyOn(api, 'apiFetch').mockResolvedValue({ version: '0.0.0' })
  vi.spyOn(api, 'fetchBackends').mockResolvedValue(list as never)
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

async function open() {
  render(<Settings onLogout={() => {}} />)
  await screen.findByText('Claude API')
}

describe('Settings AI section', () => {
  it('lists backends with model and "Key saved"', async () => {
    await open()
    expect(screen.getByText(/llama3/)).toBeTruthy()
    expect(screen.getAllByText('Key saved')).toHaveLength(1)
  })

  it('PUTs the default when the radio changes', async () => {
    const put = vi.spyOn(api, 'setDefaultBackend').mockResolvedValue({})
    await open()
    expect((screen.getByLabelText('Use Ollama by default') as HTMLInputElement).checked).toBe(true)
    fireEvent.click(screen.getByLabelText('Use Claude API by default'))
    await waitFor(() => expect(put).toHaveBeenCalledWith(2))
    expect((screen.getByLabelText('Use Claude API by default') as HTMLInputElement).checked).toBe(true)
  })

  it('Test shows seconds and a sample, or the error', async () => {
    const test = vi
      .spyOn(api, 'testBackend')
      .mockResolvedValueOnce({ ok: true, ms: 2400, sample: 'Može, vidimo se.' })
      .mockResolvedValueOnce({ ok: false, ms: 90, error: "Couldn't reach the AI" })
    await open()
    fireEvent.click(screen.getByRole('button', { name: 'Test Ollama' }))
    expect(await screen.findByText('Works · 2.4 s')).toBeTruthy()
    expect(screen.getByText('Može, vidimo se.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Test Claude API' }))
    expect(await screen.findByText("Couldn't reach the AI")).toBeTruthy()
    expect(test).toHaveBeenCalledWith(2)
  })

  it('deletes only after a confirm', async () => {
    const del = vi.spyOn(api, 'deleteBackend').mockResolvedValue({})
    await open()
    fireEvent.click(screen.getByRole('button', { name: 'Delete Claude API' }))
    expect(del).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Yes, delete' }))
    await waitFor(() => expect(del).toHaveBeenCalledWith(2))
    await waitFor(() => expect(screen.queryByText('Claude API')).toBeNull())
  })

  it('adds a backend from a preset', async () => {
    const save = vi.spyOn(api, 'saveBackend').mockResolvedValue({
      id: 3,
      kind: 'claude_cli',
      label: 'Claude (claude CLI)',
      base_url: null,
      model: 'sonnet',
      has_key: false,
    })
    await open()
    fireEvent.click(screen.getByRole('button', { name: 'Add an AI' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(save).toHaveBeenCalled())
    expect(save.mock.calls[0]![0]).toMatchObject({ kind: 'claude_cli', model: 'sonnet' })
    expect(await screen.findByText('Claude (claude CLI)')).toBeTruthy()
  })
})
