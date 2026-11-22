import { useSyncExternalStore } from 'react'
import { sendMessage, sendReaction } from './api'
import type { MessageView, QuoteView, ReactionView } from './api-types'

/** A text this device is sending. `sending` and `sent` show as an outgoing bubble, `failed` with its line and Retry. */
export type Pending = {
  /** The client_key: the idempotency key of this compose action, reused by every Retry. */
  key: string
  text: string
  replyTo: QuoteView | null
  at: string
  status: 'sending' | 'sent' | 'failed'
  error?: string
  retryable?: boolean
  /** The id the bridge gave it; the live message with this id replaces the optimistic bubble. */
  messageId?: string
}

/** My reaction on a message as the UI shows it before the server's copy agrees. "" is removed. */
export type ReactionOverride = { emoji: string; confirmed: boolean }

export type Box = {
  pending: Pending[]
  reactions: Record<string, ReactionOverride>
  /** message id -> the key its optimistic bubble had, so the live message takes over that bubble in place. */
  aliases: Record<string, string>
  /** A reaction that did not go out. */
  notice: string | null
}

const EMPTY: Box = { pending: [], reactions: {}, aliases: {}, notice: null }
const NO_ID_MS = 120_000 // a sent bubble the bridge gave no id for is dropped after this
const SKEW_MS = 60_000 // tolerated clock difference between this device and the stored message time
const SETTLE_MS = 10_000 // a confirmed reaction override is dropped after this even if no live event showed it

// Unsent and in-flight work is kept here, in memory only: it outlives the Chat screen but not a reload.
const boxes = new Map<string, Box>()
const listeners = new Set<() => void>()

export const pendingId = (key: string) => `pending:${key}`

export function getBox(jid: string): Box {
  return boxes.get(jid) ?? EMPTY
}

function update(jid: string, change: (box: Box) => Box): void {
  boxes.set(jid, change(getBox(jid)))
  listeners.forEach((l) => l())
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function useOutbox(jid: string): Box {
  return useSyncExternalStore(subscribe, () => getBox(jid))
}

export function resetOutbox(): void {
  boxes.clear()
  listeners.forEach((l) => l())
}

const inflight = new Set<string>()

function patch(jid: string, key: string, change: Partial<Pending>): void {
  update(jid, (b) => ({
    ...b,
    pending: b.pending.map((p) => (p.key === key ? { ...p, ...change } : p)),
  }))
}

async function attempt(jid: string, p: Pending): Promise<void> {
  if (inflight.has(p.key)) return // a double tap or a Retry while the first try is out must not send twice
  inflight.add(p.key)
  patch(jid, p.key, { status: 'sending', error: undefined })
  try {
    const result = await sendMessage(jid, p.text, p.replyTo?.id ?? null, p.key)
    if (result.ok) {
      patch(jid, p.key, {
        status: 'sent',
        messageId: result.message_id ?? undefined,
      })
      if (result.message_id) {
        const id = result.message_id
        update(jid, (b) => ({
          ...b,
          aliases: { ...b.aliases, [id]: pendingId(p.key) },
        }))
      } else setTimeout(() => dismiss(jid, p.key), NO_ID_MS)
    } else
      patch(jid, p.key, {
        status: 'failed',
        error: result.error,
        retryable: result.retryable,
      })
  } finally {
    inflight.delete(p.key)
  }
}

/** Puts the text in the chat as sending and sends it. Resolves when the first try has an answer. */
export function sendText(jid: string, text: string, replyTo: QuoteView | null): Promise<void> {
  const p: Pending = {
    key: crypto.randomUUID(),
    text,
    replyTo,
    at: new Date().toISOString(),
    status: 'sending',
  }
  update(jid, (b) => ({ ...b, pending: [...b.pending, p] }))
  return attempt(jid, p)
}

/** Retry with the same client_key: if the first try did go out, the bridge answers with the original message. */
export function retry(jid: string, key: string): Promise<void> {
  const p = getBox(jid).pending.find((x) => x.key === key)
  return p && p.status === 'failed' ? attempt(jid, p) : Promise.resolve()
}

export function dismiss(jid: string, key: string): void {
  update(jid, (b) => ({
    ...b,
    pending: b.pending.filter((p) => p.key !== key),
  }))
}

export function clearNotice(jid: string): void {
  update(jid, (b) => ({ ...b, notice: null }))
}

/** The emoji I currently show on a message: the optimistic one if there is one, else the server's. */
export function myReaction(m: MessageView, override?: ReactionOverride): string {
  return override ? override.emoji : (m.reactions.find((r) => r.mine)?.emoji ?? '')
}

/** The message with my optimistic reaction applied: my old chip loses one, the new one gains one. */
export function withReactions(m: MessageView, override?: ReactionOverride): MessageView {
  if (!override) return m
  const old = m.reactions.find((r) => r.mine)?.emoji ?? ''
  if (old === override.emoji) return m
  const next: ReactionView[] = m.reactions
    .map((r) => (r.mine ? { ...r, count: r.count - 1, mine: false } : r))
    .filter((r) => r.count > 0)
  if (override.emoji) {
    const same = next.find((r) => r.emoji === override.emoji)
    if (same) {
      same.count += 1
      same.mine = true
    } else next.push({ emoji: override.emoji, count: 1, mine: true })
  }
  return { ...m, reactions: next }
}

/** React with `emoji`; the same emoji again takes it back. Shown at once, reverted (with a notice) if it fails. */
export async function react(jid: string, m: MessageView, emoji: string): Promise<void> {
  const before = getBox(jid).reactions[m.id]
  const next = myReaction(m, before) === emoji ? '' : emoji
  update(jid, (b) => ({
    ...b,
    notice: null,
    reactions: { ...b.reactions, [m.id]: { emoji: next, confirmed: false } },
  }))
  const result = await sendReaction(jid, m.id, next, crypto.randomUUID())
  if (result.ok) {
    update(jid, (b) => ({
      ...b,
      reactions: { ...b.reactions, [m.id]: { emoji: next, confirmed: true } },
    }))
    setTimeout(() => settle(jid, m.id, next), SETTLE_MS)
    return
  }
  update(jid, (b) => {
    const reactions = { ...b.reactions }
    // A newer change of mine on this message owns the override now: do not undo it.
    if (reactions[m.id]?.emoji === next && !reactions[m.id].confirmed) {
      if (before) reactions[m.id] = before
      else delete reactions[m.id]
    }
    return { ...b, reactions, notice: result.error }
  })
}

function settle(jid: string, id: string, emoji: string): void {
  const current = getBox(jid).reactions[id]
  if (!current || !current.confirmed || current.emoji !== emoji) return
  update(jid, (b) => {
    const reactions = { ...b.reactions }
    delete reactions[id]
    return { ...b, reactions }
  })
}

/** The text as the server stores it: leading line breaks and trailing whitespace are trimmed before sending. */
function sentText(text: string): string {
  return text.replace(/^[\r\n]+/, '').trimEnd()
}

/** The chat's loaded messages changed: drop optimistic bubbles whose real message is in, and settle reactions the
 *  messages now show. (Aliases stay, so the real bubble keeps the optimistic one's key.) */
export function reconcile(jid: string, messages: MessageView[]): void {
  const box = getBox(jid)
  const ids = new Set(messages.map((m) => m.id))
  const gone = box.pending.filter(
    (p) =>
      (p.messageId && ids.has(p.messageId)) ||
      // No id came back: the same text from me, stored after the send, is that message.
      (p.status === 'sent' &&
        !p.messageId &&
        messages.some(
          (m) => m.from_me && m.text === sentText(p.text) && Date.parse(m.timestamp_iso) >= Date.parse(p.at) - SKEW_MS,
        )),
  )
  const settled = Object.entries(box.reactions).filter(([id, o]) => {
    const m = messages.find((x) => x.id === id)
    return o.confirmed && m !== undefined && myReaction(m) === o.emoji
  })
  if (gone.length === 0 && settled.length === 0) return
  update(jid, (b) => {
    const reactions = { ...b.reactions }
    for (const [id] of settled) delete reactions[id]
    return {
      ...b,
      pending: b.pending.filter((p) => !gone.includes(p)),
      reactions,
    }
  })
}

/** The optimistic bubble as a message, so it goes through the same list, grouping and rendering as real ones. */
export function pendingView(p: Pending, chatJid: string): MessageView {
  return {
    id: pendingId(p.key),
    chat_jid: chatJid,
    sender_jid: null,
    sender_name: null,
    from_me: true,
    timestamp_iso: p.at,
    text: p.text,
    media: null,
    reply_to: p.replyTo,
    forwarded: false,
    edited: false,
    deleted: false,
    system: false,
    reactions: [],
    poll: null,
    contact_cards: null,
    actionable: false,
    ticks: null,
  }
}

/** How a message reads when quoted: its text, or its kind for media-only ones. */
export function quoteOf(m: MessageView): QuoteView {
  const preview = m.text?.trim() || (m.media ? `[${m.media.type}]` : (m.poll?.question ?? ''))
  return {
    id: m.id,
    sender_name: m.from_me ? 'You' : m.sender_name,
    preview: preview.slice(0, 120),
  }
}
