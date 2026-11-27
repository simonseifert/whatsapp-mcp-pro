import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { Settings } from './Settings'

const stored = { context_messages: 30, own_examples: 30, about_me: 'Writes lowercase' }

beforeEach(() => {
  vi.spyOn(api, 'apiFetch').mockResolvedValue({ version: '0.0.0' })
  vi.spyOn(api, 'fetchBackends').mockResolvedValue({ backends: [], default_id: null })
  vi.spyOn(api, 'fetchDraftContext').mockResolvedValue(stored)
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

async function open() {
  render(<Settings onLogout={() => {}} />)
  await screen.findByDisplayValue('Writes lowercase')
  return screen.getByRole('button', { name: 'Save draft context' }) as HTMLButtonElement
}

describe('Settings draft context', () => {
  it('loads the saved values into the fields', async () => {
    await open()
    expect((screen.getByLabelText('Messages a draft reads') as HTMLInputElement).value).toBe('30')
    expect((screen.getByLabelText('Your past replies it learns from') as HTMLInputElement).value).toBe('30')
    expect(screen.getByText('16/1500')).toBeTruthy()
  })

  it('enables Save only after a change', async () => {
    const save = await open()
    expect(save.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Messages a draft reads'), { target: { value: '60' } })
    expect(save.disabled).toBe(false)
    expect(screen.getByText('60')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Messages a draft reads'), { target: { value: '30' } })
    expect(save.disabled).toBe(true)
  })

  it('saves only the changed fields', async () => {
    const put = vi.spyOn(api, 'saveDraftContext').mockResolvedValue({ ...stored, own_examples: 80 })
    const save = await open()
    fireEvent.change(screen.getByLabelText('Your past replies it learns from'), { target: { value: '80' } })
    fireEvent.click(save)
    await waitFor(() => expect(put).toHaveBeenCalledWith({ own_examples: 80 }))
    await waitFor(() => expect(save.disabled).toBe(true))
  })

  it('shows the error from a 422', async () => {
    vi.spyOn(api, 'saveDraftContext').mockRejectedValue(new api.ApiError(422, "'about_me' can be at most 1500 characters"))
    const save = await open()
    fireEvent.change(screen.getByLabelText('About me'), { target: { value: 'changed' } })
    fireEvent.click(save)
    expect(await screen.findByText("'about_me' can be at most 1500 characters")).toBeTruthy()
    expect(save.disabled).toBe(false)
  })
})
