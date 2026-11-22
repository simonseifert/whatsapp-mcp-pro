import type { MessageView } from './api-types'
import { dayKey, formatDayLabel } from './format'

export type Item =
  | { kind: 'day'; key: string; label: string }
  | { kind: 'system'; key: string; message: MessageView }
  | { kind: 'message'; key: string; message: MessageView; showSender: boolean; tail: boolean }

// Our own messages can be filed under either JID form, so for them "same sender" is just "from me".
const sameRun = (a: MessageView, b: MessageView) =>
  !a.system &&
  !b.system &&
  dayKey(a.timestamp_iso) === dayKey(b.timestamp_iso) &&
  (a.from_me ? b.from_me : !b.from_me && a.sender_jid === b.sender_jid)

/** Messages (oldest first) to day separators, system notes and bubbles; the tail goes on the last of a run and the
 *  sender name on the first, in groups only. */
export function toItems(messages: MessageView[], inGroup: boolean, now: Date = new Date()): Item[] {
  const items: Item[] = []
  messages.forEach((m, i) => {
    const prev = messages[i - 1]
    const next = messages[i + 1]
    if (!prev || dayKey(prev.timestamp_iso) !== dayKey(m.timestamp_iso)) {
      items.push({ kind: 'day', key: `day-${dayKey(m.timestamp_iso)}`, label: formatDayLabel(m.timestamp_iso, now) })
    }
    const key = `${m.chat_jid}/${m.id}`
    if (m.system) {
      items.push({ kind: 'system', key, message: m })
      return
    }
    items.push({
      kind: 'message',
      key,
      message: m,
      showSender: inGroup && !m.from_me && !(prev && sameRun(prev, m)),
      tail: !(next && sameRun(m, next)),
    })
  })
  return items
}
