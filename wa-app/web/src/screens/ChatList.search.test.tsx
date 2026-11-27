import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import * as live from '../live'
import { ChatList } from './ChatList'

function Where() {
  const l = useLocation()
  return <p data-testid="where">{l.pathname + l.search}</p>
}

describe('Chat list: searching messages', () => {
  beforeEach(() => {
    vi.spyOn(live, 'subscribeLive').mockImplementation(() => () => {})
    vi.spyOn(api, 'apiFetch').mockResolvedValue({ chats: [], has_more: false, archived_count: 0 })
  })
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('offers a message search for what was typed and opens it', async () => {
    render(
      <MemoryRouter>
        <Where />
        <ChatList />
      </MemoryRouter>,
    )
    expect(screen.queryByText(/Search messages for/)).toBeNull()
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'račun' } })
    fireEvent.click(await screen.findByRole('link', { name: /Search messages for/ }))
    expect(screen.getByTestId('where').textContent).toBe(`/search?q=${encodeURIComponent('račun')}`)
  })

  it('Enter opens the message search too, but one letter does not', async () => {
    render(
      <MemoryRouter>
        <Where />
        <ChatList />
      </MemoryRouter>,
    )
    const box = screen.getByRole('searchbox')
    fireEvent.change(box, { target: { value: 'r' } })
    fireEvent.submit(box)
    expect(screen.getByTestId('where').textContent).toBe('/')
    fireEvent.change(box, { target: { value: 'rač' } })
    fireEvent.submit(box)
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe(`/search?q=${encodeURIComponent('rač')}`))
  })
})
