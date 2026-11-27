import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { ChatList } from '../api-types'
import { Lists } from './Lists'

const row = (id: number, name: string, position: number): ChatList => ({ id, name, position, count: 0 })

describe('Lists screen', () => {
  let state: ChatList[]
  beforeEach(() => {
    state = [row(1, 'Work', 0), row(2, 'Family', 1)]
    vi.spyOn(api, 'fetchLists').mockImplementation(() => Promise.resolve(structuredClone(state)))
    vi.spyOn(api, 'createList').mockImplementation((name) => {
      const made = row(3, name, state.length)
      state = [...state, made]
      return Promise.resolve(made)
    })
    vi.spyOn(api, 'updateList').mockResolvedValue(undefined)
    vi.spyOn(api, 'deleteList').mockResolvedValue(undefined)
  })
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('shows the lists in saved order', async () => {
    render(<Lists onBack={() => {}} />)
    expect((await screen.findAllByRole('listitem')).map((li) => li.textContent)).toEqual([
      expect.stringContaining('Work'),
      expect.stringContaining('Family'),
    ])
  })

  it('creates a list', async () => {
    render(<Lists onBack={() => {}} />)
    await screen.findByText('Work')
    fireEvent.change(screen.getByLabelText('New list name'), { target: { value: 'Friends' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add list' }))
    await waitFor(() => expect(api.createList).toHaveBeenCalledWith('Friends'))
    expect(await screen.findByText('Friends')).toBeTruthy()
  })

  it('shows the server message when the name is taken', async () => {
    vi.spyOn(api, 'createList').mockRejectedValue(new api.ApiError(409, 'You already have a list with that name'))
    render(<Lists onBack={() => {}} />)
    await screen.findByText('Work')
    fireEvent.change(screen.getByLabelText('New list name'), { target: { value: 'work' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add list' }))
    expect((await screen.findByRole('alert')).textContent).toBe('You already have a list with that name')
  })

  it('renames a list', async () => {
    render(<Lists onBack={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Rename Work' }))
    fireEvent.change(screen.getByLabelText('List name'), { target: { value: 'Office' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.updateList).toHaveBeenCalledWith(1, { name: 'Office' }))
  })

  it('moves a list down and up', async () => {
    render(<Lists onBack={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Move Work down' }))
    await waitFor(() => expect(api.updateList).toHaveBeenCalledWith(1, { position: 1 }))
    fireEvent.click(await screen.findByRole('button', { name: 'Move Family up' }))
    await waitFor(() => expect(api.updateList).toHaveBeenCalledWith(2, { position: 0 }))
  })

  it('does not offer to move the first list up or the last one down', async () => {
    render(<Lists onBack={() => {}} />)
    await screen.findByText('Work')
    expect((screen.getByRole('button', { name: 'Move Work up' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Move Family down' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('deletes only after a confirmation', async () => {
    render(<Lists onBack={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Delete Work' }))
    expect(api.deleteList).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Yes, delete' }))
    await waitFor(() => expect(api.deleteList).toHaveBeenCalledWith(1))
  })
})
