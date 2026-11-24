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
})
