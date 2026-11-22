import { describe, expect, it } from 'vitest'
import type { MessageView } from './api-types'
import { toItems } from './chatItems'

const base: MessageView = {
  id: '', chat_jid: 'c', sender_jid: 'a', sender_name: 'Ana', from_me: false, timestamp_iso: '', text: 'x', media: null,
  reply_to: null, forwarded: false, edited: false, deleted: false, system: false, reactions: [], poll: null,
  contact_cards: null, actionable: true, ticks: null,
}
const msg = (id: string, at: Date, extra: Partial<MessageView> = {}): MessageView => ({
  ...base, id, timestamp_iso: at.toISOString(), ...extra,
})
const now = new Date(2026, 10, 22, 12, 0)

describe('toItems', () => {
  it('puts the tail on the last of a run, the sender on the first, and separators between days', () => {
    const items = toItems(
      [
        msg('1', new Date(2026, 10, 21, 19, 0)),
        msg('2', new Date(2026, 10, 21, 19, 1)),
        msg('3', new Date(2026, 10, 21, 19, 2), { sender_jid: 'b', sender_name: 'Bo' }),
        msg('4', new Date(2026, 10, 22, 9, 0), { sender_jid: 'b', sender_name: 'Bo' }),
        msg('5', new Date(2026, 10, 22, 9, 1), { from_me: true, sender_jid: 'me1' }),
        msg('6', new Date(2026, 10, 22, 9, 2), { from_me: true, sender_jid: 'me2' }),
      ],
      true,
      now,
    )
    expect(items.map((i) => (i.kind === 'day' ? i.label : i.kind === 'message' ? i.message.id : 'sys'))).toEqual([
      'Yesterday', '1', '2', '3', 'Today', '4', '5', '6',
    ])
    const m = (id: string) => items.find((i) => i.kind === 'message' && i.message.id === id)
    expect(m('1')).toMatchObject({ showSender: true, tail: false })
    expect(m('2')).toMatchObject({ showSender: false, tail: true })
    expect(m('3')).toMatchObject({ showSender: true, tail: true }) // day change ends the run
    expect(m('4')).toMatchObject({ showSender: true, tail: true })
    expect(m('5')).toMatchObject({ showSender: false, tail: false }) // own messages: one run across JID forms
    expect(m('6')).toMatchObject({ showSender: false, tail: true })
  })

  it('shows no sender names in a private chat and keeps system notes out of runs', () => {
    const items = toItems(
      [msg('1', new Date(2026, 10, 22, 9, 0)), msg('2', new Date(2026, 10, 22, 9, 1), { system: true })],
      false,
      now,
    )
    expect(items.find((i) => i.kind === 'message')).toMatchObject({ showSender: false, tail: true })
    expect(items.some((i) => i.kind === 'system')).toBe(true)
  })
})
