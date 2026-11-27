import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
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
