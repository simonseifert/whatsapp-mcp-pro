import type { MessageView } from './api-types'

const keyOf = (m: MessageView) => `${m.chat_jid}/${m.id}`
const at = (m: MessageView) => Date.parse(m.timestamp_iso)

export type Placed = {
  messages: MessageView[]
  /** The message was not in the list before. */
  added: boolean
  /** It is now the last (newest) message of the chat. Only then does it rise in or raise the "New messages" pill. */
  newest: boolean
}

/**
 * Puts a message that arrived over the live stream into a chat's loaded messages (oldest first).
 * New rows are not always new messages (history sync and backfill add old ones, a replaced row comes again), so it is
 * placed by timestamp and deduped by (chat_jid, id). A message older than everything loaded while older pages
 * exist is left to paging.
 */
export function placeMessage(messages: MessageView[], incoming: MessageView, hasMore: boolean): Placed {
  const key = keyOf(incoming)
  const existing = messages.findIndex((m) => keyOf(m) === key)
  if (existing >= 0) {
    const next = messages.slice()
    next[existing] = incoming
    return { messages: next, added: false, newest: false }
  }
  const time = at(incoming)
  if (hasMore && messages.length > 0 && time < at(messages[0])) return { messages, added: false, newest: false }
  let index = messages.length
  while (index > 0 && at(messages[index - 1]) > time) index -= 1 // after equal timestamps: arrival order
  const next = [...messages.slice(0, index), incoming, ...messages.slice(index)]
  return { messages: next, added: true, newest: index === messages.length }
}

/** Overlays a freshly fetched newest page on what is loaded: its messages replace or join, nothing older is lost. */
export function mergePage(messages: MessageView[], page: MessageView[]): MessageView[] {
  const fresh = new Map(page.map((m) => [keyOf(m), m]))
  const merged = messages.map((m) => fresh.get(keyOf(m)) ?? m)
  const known = new Set(messages.map(keyOf))
  return [...merged, ...page.filter((p) => !known.has(keyOf(p)))].sort((a, b) => at(a) - at(b))
}
