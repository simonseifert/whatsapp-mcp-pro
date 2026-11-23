import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { DraftView } from '../api-types'
import { DraftCard } from './DraftCard'

const draft = (extra: Partial<DraftView> = {}): DraftView => ({
  id: 4,
  chat_jid: 'a@s.whatsapp.net',
  trigger_id: 'M1',
  status: 'ready',
  alternatives: ['Stižem za deset minuta', 'Kasnim malo'],
  error: null,
  quote: { id: 'M1', sender_name: 'Nikola', preview: 'Gdje si?' },
  created_at_iso: '2026-11-23T10:00:00+00:00',
  ...extra,
})

function setup(d: DraftView) {
  const props = { onUse: vi.fn(), onDismiss: vi.fn(), onRetry: vi.fn() }
  render(<DraftCard draft={d} name="Nikola" {...props} />)
  return props
}

afterEach(cleanup)

describe('DraftCard', () => {
  it('shows the label, the quote and each alternative as a tappable row', () => {
    const { onUse } = setup(draft())
    expect(screen.getByText('Draft · replying to Nikola')).toBeTruthy()
    expect(screen.getByText('Gdje si?')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Kasnim malo/ }))
    expect(onUse).toHaveBeenCalledWith('Kasnim malo')
    expect(screen.getAllByText('Use')).toHaveLength(2)
  })

  it('has no Send button', () => {
    setup(draft())
    expect(screen.queryByRole('button', { name: /send/i })).toBeNull()
  })

  it('shows Drafting… while pending', () => {
    setup(draft({ status: 'pending', alternatives: [] }))
    expect(screen.getByText('Drafting…')).toBeTruthy()
  })

  it('shows the error and Try again when failed', () => {
    const { onRetry } = setup(draft({ status: 'failed', alternatives: [], error: 'This chat is local only' }))
    expect(screen.getByText('This chat is local only')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(onRetry).toHaveBeenCalled()
  })

  it('dismisses with the × button', () => {
    const { onDismiss } = setup(draft())
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss draft' }))
    expect(onDismiss).toHaveBeenCalled()
  })
})
