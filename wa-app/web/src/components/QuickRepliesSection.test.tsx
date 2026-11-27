import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { QuickReply } from '../api-types'
import { QuickRepliesSection } from './QuickRepliesSection'

describe('Settings quick replies', () => {
  let state: QuickReply[]
  beforeEach(() => {
    state = [{ shortcut: 'thanks', text: 'Thanks a lot', used: 2 }]
    vi.spyOn(api, 'fetchQuickReplies').mockImplementation(() => Promise.resolve(structuredClone(state)))
    vi.spyOn(api, 'createQuickReply').mockImplementation((shortcut, text) => {
      state = [...state, { shortcut, text, used: 0 }]
      return Promise.resolve()
    })
    vi.spyOn(api, 'updateQuickReply').mockResolvedValue(undefined)
    vi.spyOn(api, 'deleteQuickReply').mockResolvedValue(undefined)
  })
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('lists, adds, edits and deletes', async () => {
    render(<QuickRepliesSection />)
    expect(await screen.findByText('/thanks')).toBeTruthy()
    expect(screen.getByText('Thanks a lot')).toBeTruthy()

    fireEvent.change(screen.getByLabelText('New shortcut'), { target: { value: 'omw' } })
    fireEvent.change(screen.getByLabelText('New text'), { target: { value: 'On my way' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add quick reply' }))
    await waitFor(() => expect(api.createQuickReply).toHaveBeenCalledWith('omw', 'On my way'))
    expect(await screen.findByText('/omw')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Edit /thanks' }))
    fireEvent.change(screen.getByLabelText('Text'), { target: { value: 'Thank you' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.updateQuickReply).toHaveBeenCalledWith('thanks', 'Thank you'))

    fireEvent.click(screen.getByRole('button', { name: 'Delete /thanks' }))
    fireEvent.click(screen.getByRole('button', { name: 'Yes, delete' }))
    await waitFor(() => expect(api.deleteQuickReply).toHaveBeenCalledWith('thanks'))
  })

  it('refuses a bad shortcut before asking the server', async () => {
    render(<QuickRepliesSection />)
    await screen.findByText('/thanks')
    fireEvent.change(screen.getByLabelText('New shortcut'), { target: { value: 'Bad Name' } })
    fireEvent.change(screen.getByLabelText('New text'), { target: { value: 'x' } })
    expect((screen.getByRole('button', { name: 'Add quick reply' }) as HTMLButtonElement).disabled).toBe(true)
    expect(api.createQuickReply).not.toHaveBeenCalled()
  })

  it('shows the server message when the shortcut is taken', async () => {
    vi.spyOn(api, 'createQuickReply').mockRejectedValue(new api.ApiError(409, 'You already have a quick reply with that shortcut'))
    render(<QuickRepliesSection />)
    await screen.findByText('/thanks')
    fireEvent.change(screen.getByLabelText('New shortcut'), { target: { value: 'thanks' } })
    fireEvent.change(screen.getByLabelText('New text'), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add quick reply' }))
    expect((await screen.findByRole('alert')).textContent).toContain('already have')
  })
})
