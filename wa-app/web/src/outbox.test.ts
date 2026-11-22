import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from './api'
import type { MessageView } from './api-types'
import { dismiss, getBox, react, reconcile, resetOutbox, retry, sendText, withReactions } from './outbox'

const JID = 'a@s.whatsapp.net'
const ok = { ok: true, message_id: 'SRV1' } as const
const fail = { ok: false, error: 'Not sent: limit', retryable: true } as const

function msg(id: string, extra: Partial<MessageView> = {}): MessageView {
  return {
    id,
    chat_jid: JID,
    sender_jid: JID,
    sender_name: 'Ana',
    from_me: false,
    timestamp_iso: '2026-11-22T10:00:00+00:00',
    text: 'hej',
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
    ...extra,
  }
}

describe('outbox', () => {
  let send: ReturnType<typeof vi.spyOn>
  let reactApi: ReturnType<typeof vi.spyOn>
  beforeEach(() => {
    resetOutbox()
    send = vi.spyOn(api, 'sendMessage').mockResolvedValue(ok)
    reactApi = vi.spyOn(api, 'sendReaction').mockResolvedValue({ ok: true, message_id: null })
  })
  afterEach(() => vi.restoreAllMocks())

  it('shows a sending bubble at once, then keeps the server id until the live message replaces it', async () => {
    const done = sendText(JID, 'Bok', null)
    expect(getBox(JID).pending).toMatchObject([{ text: 'Bok', status: 'sending' }])
    await done
    const [p] = getBox(JID).pending
    expect(p).toMatchObject({ status: 'sent', messageId: 'SRV1' })
    expect(getBox(JID).aliases).toEqual({ SRV1: `pending:${p.key}` })
    reconcile(JID, [msg('SRV1', { from_me: true })])
    expect(getBox(JID).pending).toEqual([])
    expect(getBox(JID).aliases).toHaveProperty('SRV1') // the bubble keeps its key: no remount, no flicker
  })

  it('sends the replied-to id and a uuid key', async () => {
    await sendText(JID, 'Da', { id: 'Q1', sender_name: 'Ana', preview: 'hej' })
    expect(send).toHaveBeenCalledWith(JID, 'Da', 'Q1', expect.stringMatching(/^[0-9a-f-]{36}$/))
  })

  it('a failure stays visible with the line, and Retry reuses the same key', async () => {
    send.mockResolvedValueOnce(fail)
    await sendText(JID, 'Bok', null)
    const [p] = getBox(JID).pending
    expect(p).toMatchObject({
      status: 'failed',
      error: 'Not sent: limit',
      retryable: true,
    })
    await retry(JID, p.key)
    expect(send).toHaveBeenCalledTimes(2)
    expect(send.mock.calls[1][3]).toBe(send.mock.calls[0][3])
    expect(getBox(JID).pending[0].status).toBe('sent')
  })

  it('a retry while the first attempt is still out does not send a second time', async () => {
    let release: (r: api.SendResult) => void = () => undefined
    send.mockReturnValueOnce(new Promise<api.SendResult>((r) => (release = r)))
    void sendText(JID, 'Bok', null)
    const [p] = getBox(JID).pending
    await retry(JID, p.key)
    expect(send).toHaveBeenCalledTimes(1)
    release(ok)
  })

  it('dismiss drops a failed draft', async () => {
    send.mockResolvedValueOnce(fail)
    await sendText(JID, 'Bok', null)
    dismiss(JID, getBox(JID).pending[0].key)
    expect(getBox(JID).pending).toEqual([])
  })

  it('failed drafts live in memory per chat and survive re-reading the box', async () => {
    send.mockResolvedValueOnce(fail)
    await sendText(JID, 'Bok', null)
    expect(getBox(JID)).toBe(getBox(JID))
    expect(getBox('other@s.whatsapp.net').pending).toEqual([])
  })

  it('reacts optimistically, toggles the same emoji off with an empty one, and reverts on failure', async () => {
    const m = msg('M1')
    await react(JID, m, '👍')
    expect(reactApi).toHaveBeenLastCalledWith(JID, 'M1', '👍', expect.any(String))
    expect(withReactions(m, getBox(JID).reactions.M1).reactions).toEqual([{ emoji: '👍', count: 1, mine: true }])
    await react(JID, m, '👍')
    expect(reactApi).toHaveBeenLastCalledWith(JID, 'M1', '', expect.any(String))
    reactApi.mockResolvedValueOnce(fail)
    await react(JID, m, '❤️')
    expect(getBox(JID).reactions.M1.emoji).toBe('') // reverted to the last good state (removed)
    expect(getBox(JID).notice).toBe('Not sent: limit')
  })

  it('withReactions swaps my reaction and drops a chip that reaches zero', () => {
    const m = msg('M1', {
      reactions: [
        { emoji: '👍', count: 1, mine: true },
        { emoji: '❤️', count: 2, mine: false },
      ],
    })
    const swapped = withReactions(m, { emoji: '❤️', confirmed: false })
    expect(swapped.reactions).toEqual([{ emoji: '❤️', count: 3, mine: true }])
    expect(withReactions(m, { emoji: '', confirmed: false }).reactions).toEqual([
      { emoji: '❤️', count: 2, mine: false },
    ])
  })

  it('reconcile settles a confirmed reaction once the message shows it', async () => {
    const m = msg('M1')
    await react(JID, m, '👍')
    reconcile(JID, [m])
    expect(getBox(JID).reactions.M1).toBeDefined()
    reconcile(JID, [msg('M1', { reactions: [{ emoji: '👍', count: 1, mine: true }] })])
    expect(getBox(JID).reactions.M1).toBeUndefined()
  })

  it('a sent bubble without an id is dropped once a matching message of mine appears after it', async () => {
    send.mockResolvedValueOnce({ ok: true, message_id: null })
    await sendText(JID, 'Bok', null)
    expect(getBox(JID).pending).toHaveLength(1)
    const at = getBox(JID).pending[0].at
    reconcile(JID, [msg('OLD', { from_me: true, text: 'Bok', timestamp_iso: '2020-01-01T00:00:00+00:00' })])
    expect(getBox(JID).pending).toHaveLength(1)
    reconcile(JID, [msg('NEW', { from_me: true, text: 'Bok', timestamp_iso: at })])
    expect(getBox(JID).pending).toEqual([])
  })

  it('a sent bubble without an id matches the text the server stored (trimmed like the server does)', async () => {
    send.mockResolvedValueOnce({ ok: true, message_id: null })
    await sendText(JID, '\n\nBok  \n', null)
    const at = getBox(JID).pending[0].at
    reconcile(JID, [msg('NEW', { from_me: true, text: 'Bok', timestamp_iso: at })])
    expect(getBox(JID).pending).toEqual([])
  })

  it('a sent bubble without an id is dropped after two minutes', async () => {
    vi.useFakeTimers()
    try {
      send.mockResolvedValueOnce({ ok: true, message_id: null })
      await sendText(JID, 'Bok', null)
      await vi.advanceTimersByTimeAsync(119_000)
      expect(getBox(JID).pending).toHaveLength(1)
      await vi.advanceTimersByTimeAsync(2_000)
      expect(getBox(JID).pending).toEqual([])
    } finally {
      vi.useRealTimers()
    }
  })

  it('a failed reaction only reverts when the override is still its own', async () => {
    const m = msg('M1')
    let release: (r: api.SendResult) => void = () => undefined
    reactApi.mockReturnValueOnce(new Promise<api.SendResult>((r) => (release = r)))
    const first = react(JID, m, '👍')
    await react(JID, m, '❤️') // a second change while the first is out
    release(fail)
    await first
    expect(getBox(JID).reactions.M1.emoji).toBe('❤️')
    expect(getBox(JID).notice).toBe('Not sent: limit')
  })
})
