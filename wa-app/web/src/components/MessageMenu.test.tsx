import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { MessageView } from '../api-types'
import { MessageMenu } from './MessageMenu'

const NOW = Date.parse('2026-11-27T12:00:00Z')
const sentAgo = (seconds: number) => new Date(NOW - seconds * 1000).toISOString()
const base: MessageView = {
  id: 'M1',
  chat_jid: 'a@s.whatsapp.net',
  sender_jid: null,
  sender_name: null,
  from_me: true,
  timestamp_iso: sentAgo(60),
  text: 'Bok',
  media: null,
  reply_to: null,
  forwarded: false,
  edited: false,
  deleted: false,
  system: false,
  reactions: [],
  poll: null,
  contact_cards: null,
  actionable: true,
  ticks: null,
}
const handlers = { onReact: vi.fn(), onReply: vi.fn(), onCopy: vi.fn(), onClose: vi.fn() }
const open = (m: Partial<MessageView>, extra: Record<string, unknown> = {}) =>
  render(<MessageMenu message={{ ...base, ...m }} mine="" now={NOW} {...handlers} {...extra} />)
const item = (name: string) => screen.queryByRole('button', { name })

afterEach(cleanup)

describe('MessageMenu actions', () => {
  it('offers Edit at 14:59 after sending and not at 15:01', () => {
    open({ timestamp_iso: sentAgo(14 * 60 + 59) }, { onEdit: vi.fn() })
    expect(item('Edit')).toBeTruthy()
    cleanup()
    open({ timestamp_iso: sentAgo(15 * 60 + 1) }, { onEdit: vi.fn() })
    expect(item('Edit')).toBeNull()
  })

  it('offers Delete for everyone up to 48 hours and not after', () => {
    open({ timestamp_iso: sentAgo(48 * 3600 - 60) }, { onRevoke: vi.fn() })
    expect(item('Delete for everyone')).toBeTruthy()
    cleanup()
    open({ timestamp_iso: sentAgo(48 * 3600 + 60) }, { onRevoke: vi.fn() })
    expect(item('Delete for everyone')).toBeNull()
  })

  it('does not offer Forward on a contact card', () => {
    open({ text: null, contact_cards: [{ name: 'Ivo', phones: ['+385 91 000 0777'] }] }, { onForward: vi.fn() })
    expect(item('Forward')).toBeNull()
    cleanup()
    open({}, { onForward: vi.fn() })
    expect(item('Forward')).toBeTruthy()
  })

  it('never offers Edit or Delete for everyone on someone else\'s message, nor Edit on media', () => {
    open({ from_me: false }, { onEdit: vi.fn(), onRevoke: vi.fn() })
    expect(item('Edit')).toBeNull()
    expect(item('Delete for everyone')).toBeNull()
    cleanup()
    open({ media: { type: 'image', filename: null, duration_s: null, has_file: true, downloadable: false, size_bytes: 1 } }, { onEdit: vi.fn() })
    expect(item('Edit')).toBeNull()
  })

  it('Star becomes Unstar on a starred message and each item calls its handler', () => {
    const onStar = vi.fn()
    const onForward = vi.fn()
    const onEdit = vi.fn()
    const onRevoke = vi.fn()
    open({ starred: true }, { onStar, onForward, onEdit, onRevoke })
    expect(item('Star')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Unstar' }))
    fireEvent.click(screen.getByRole('button', { name: 'Forward' }))
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete for everyone' }))
    expect([onStar, onForward, onEdit, onRevoke].map((f) => f.mock.calls.length)).toEqual([1, 1, 1, 1])
  })

  it('an imported row gets none of the WhatsApp actions', () => {
    open({ actionable: false }, { onStar: vi.fn(), onForward: vi.fn(), onEdit: vi.fn(), onRevoke: vi.fn() })
    for (const name of ['Star', 'Forward', 'Edit', 'Delete for everyone']) expect(item(name)).toBeNull()
  })
})
