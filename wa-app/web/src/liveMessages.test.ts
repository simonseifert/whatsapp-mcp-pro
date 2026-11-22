import { describe, expect, it } from 'vitest'
import type { MessageView } from './api-types'
import { mergePage, placeMessage } from './liveMessages'

const msg = (id: string, iso: string, extra: Partial<MessageView> = {}): MessageView =>
  ({ id, chat_jid: 'c', timestamp_iso: iso, text: id, ...extra }) as MessageView

const a = msg('a', '2026-11-22T10:00:00+01:00')
const b = msg('b', '2026-11-22T11:00:00+01:00')

describe('placeMessage', () => {
  it('appends the newest message and says so', () => {
    const r = placeMessage([a, b], msg('c', '2026-11-22T12:00:00+01:00'), false)
    expect(r.messages.map((m) => m.id)).toEqual(['a', 'b', 'c'])
    expect(r).toMatchObject({ added: true, newest: true })
  })

  it('places a history message by time and does not call it newest', () => {
    const r = placeMessage([a, b], msg('old', '2026-11-22T10:30:00+01:00'), false)
    expect(r.messages.map((m) => m.id)).toEqual(['a', 'old', 'b'])
    expect(r).toMatchObject({ added: true, newest: false })
  })

  it('compares instants, not strings, across UTC offsets', () => {
    const r = placeMessage([a, b], msg('x', '2026-11-22T09:30:00+00:00'), false) // 10:30 in +01:00
    expect(r.messages.map((m) => m.id)).toEqual(['a', 'x', 'b'])
  })

  it('replaces a message it already has, even when the row came again', () => {
    const r = placeMessage([a, b], msg('a', '2026-11-22T10:00:00+01:00', { text: 'changed' }), false)
    expect(r.messages).toHaveLength(2)
    expect(r.messages[0].text).toBe('changed')
    expect(r).toMatchObject({ added: false, newest: false })
  })

  it('leaves a message older than the loaded window to paging', () => {
    const r = placeMessage([a, b], msg('ancient', '2020-01-01T00:00:00+00:00'), true)
    expect(r.messages).toEqual([a, b])
    expect(placeMessage([a, b], msg('ancient', '2020-01-01T00:00:00+00:00'), false).messages[0].id).toBe('ancient')
  })

  it('the first message of an empty chat is the newest', () => {
    expect(placeMessage([], a, false)).toMatchObject({ added: true, newest: true })
  })
})

describe('mergePage', () => {
  it('keeps older loaded messages, refreshes known ones and adds new ones in time order', () => {
    const merged = mergePage([a, b], [msg('b', b.timestamp_iso, { text: 'fresh' }), msg('c', '2026-11-22T12:00:00+01:00')])
    expect(merged.map((m) => m.id)).toEqual(['a', 'b', 'c'])
    expect(merged[1].text).toBe('fresh')
  })
})
