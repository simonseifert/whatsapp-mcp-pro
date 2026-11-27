import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { QuickReply } from '../api-types'
import { filterReplies, QuickReplyPicker } from './QuickReplyPicker'

const replies: QuickReply[] = [
  { shortcut: 'thanks', text: 'Thanks a lot', used: 3 },
  { shortcut: 'thx', text: 'Thx', used: 3 },
  { shortcut: 'omw', text: 'On my way', used: 0 },
]

afterEach(cleanup)

describe('filterReplies', () => {
  it('matches the shortcut by prefix, case-insensitively, and keeps the order the server sent (used, then shortcut)', () => {
    expect(filterReplies(replies, '').map((r) => r.shortcut)).toEqual(['thanks', 'thx', 'omw'])
    expect(filterReplies(replies, 'TH').map((r) => r.shortcut)).toEqual(['thanks', 'thx'])
    expect(filterReplies(replies, 'om').map((r) => r.shortcut)).toEqual(['omw'])
    expect(filterReplies(replies, 'zzz')).toEqual([])
  })
})

describe('QuickReplyPicker', () => {
  it('lists the matches with their text and picks one on tap', () => {
    const onPick = vi.fn()
    render(<QuickReplyPicker replies={filterReplies(replies, 'o')} onPick={onPick} />)
    const option = screen.getByRole('option', { name: /omw/ })
    expect(option.textContent).toContain('On my way')
    fireEvent.click(option)
    expect(onPick).toHaveBeenCalledWith(replies[2])
  })

  it('says so when nothing matches', () => {
    render(<QuickReplyPicker replies={[]} onPick={() => {}} />)
    expect(screen.getByText('No quick replies match')).toBeTruthy()
  })
})
