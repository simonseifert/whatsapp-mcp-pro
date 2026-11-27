import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { FilterChips } from './FilterChips'

afterEach(cleanup)

describe('FilterChips', () => {
  it('shows the built-in chips until the server answers', () => {
    render(<FilterChips chips={null} value="all" onChange={() => {}} />)
    expect(screen.getAllByRole('button').map((b) => b.textContent)).toEqual(['All', 'Unread', 'Favourites', 'Groups', 'Mentions and replies'])
  })

  it('shows the server chips in their order, custom lists last', () => {
    const chips = [
      { id: 'all', label: 'All' },
      { id: 'mentions', label: 'Mentions and replies' },
      { id: 'list:2', label: 'Family' },
      { id: 'list:1', label: 'Work' },
    ]
    const onChange = vi.fn()
    render(<FilterChips chips={chips} value="list:2" onChange={onChange} />)
    expect(screen.getAllByRole('button').map((b) => b.textContent)).toEqual(['All', 'Mentions and replies', 'Family', 'Work'])
    expect(screen.getByRole('button', { name: 'Family' }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(screen.getByRole('button', { name: 'Work' }))
    expect(onChange).toHaveBeenCalledWith('list:1')
  })
})
