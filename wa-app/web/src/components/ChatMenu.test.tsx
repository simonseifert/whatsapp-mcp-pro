import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ChatMenu } from './ChatMenu'

afterEach(cleanup)

describe('ChatMenu', () => {
  it('is anchored to the kebab at the top right', () => {
    const { container } = render(<ChatMenu onAi={() => {}} onClose={() => {}} />)
    expect(container.querySelector('.menu-wrap')?.classList.contains('menu-wrap-top-right')).toBe(true)
    expect(screen.getByRole('button', { name: 'AI drafts' })).toBeTruthy()
  })

  it('offers a search only when the chat can open one', () => {
    const { rerender } = render(<ChatMenu onAi={() => {}} onClose={() => {}} />)
    expect(screen.queryByRole('button', { name: 'Search' })).toBeNull()
    rerender(<ChatMenu onAi={() => {}} onSearch={() => {}} onClose={() => {}} />)
    expect(screen.getByRole('button', { name: 'Search' })).toBeTruthy()
  })
})

describe('ChatMenu lists', () => {
  it('offers Add to list when the chat can open it', () => {
    const { rerender } = render(<ChatMenu onAi={() => {}} onClose={() => {}} />)
    expect(screen.queryByRole('button', { name: 'Add to list' })).toBeNull()
    rerender(<ChatMenu onAi={() => {}} onLists={() => {}} onClose={() => {}} />)
    expect(screen.getByRole('button', { name: 'Add to list' })).toBeTruthy()
  })
})

describe('ChatMenu chat actions', () => {
  const state = { pinned: false, muted: false, archived: false, marked_unread: false }

  it('offers pin, mute, archive and mark as unread', () => {
    const onChange = vi.fn()
    render(<ChatMenu state={state} onChange={onChange} onClose={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Pin chat' }))
    expect(onChange).toHaveBeenLastCalledWith({ pin: true })
    fireEvent.click(screen.getByRole('button', { name: 'Archive chat' }))
    expect(onChange).toHaveBeenLastCalledWith({ archive: true })
    fireEvent.click(screen.getByRole('button', { name: 'Mark as unread' }))
    expect(onChange).toHaveBeenLastCalledWith({ unread: true })
  })

  it('asks how long to mute, then sends the choice', () => {
    const onChange = vi.fn()
    render(<ChatMenu state={state} onChange={onChange} onClose={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Mute notifications' }))
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '8 hours' }))
    expect(onChange).toHaveBeenLastCalledWith({ mute: '8h' })
  })

  it('maps the other mute choices', () => {
    const onChange = vi.fn()
    render(<ChatMenu state={state} onChange={onChange} onClose={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Mute notifications' }))
    fireEvent.click(screen.getByRole('button', { name: '1 week' }))
    expect(onChange).toHaveBeenLastCalledWith({ mute: '1w' })
    cleanup()
    render(<ChatMenu state={state} onChange={onChange} onClose={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Mute notifications' }))
    fireEvent.click(screen.getByRole('button', { name: 'Always' }))
    expect(onChange).toHaveBeenLastCalledWith({ mute: 'forever' })
  })

  it('shows the opposite action for a chat that is already pinned, muted, archived or marked', () => {
    const onChange = vi.fn()
    render(
      <ChatMenu
        state={{ pinned: true, muted: true, archived: true, marked_unread: true }}
        onChange={onChange}
        onClose={() => {}}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Unpin chat' }))
    expect(onChange).toHaveBeenLastCalledWith({ pin: false })
    fireEvent.click(screen.getByRole('button', { name: 'Unmute notifications' }))
    expect(onChange).toHaveBeenLastCalledWith({ mute: false })
    fireEvent.click(screen.getByRole('button', { name: 'Unarchive chat' }))
    expect(onChange).toHaveBeenLastCalledWith({ archive: false })
    fireEvent.click(screen.getByRole('button', { name: 'Mark as read' }))
    expect(onChange).toHaveBeenLastCalledWith({ unread: false })
  })
})
