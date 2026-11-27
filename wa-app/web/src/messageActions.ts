import type { MessageView } from './api-types'

/** WhatsApp's limits: edit a sent text for 15 minutes, delete it for everyone for 48 hours. The server enforces both. */
export const EDIT_WINDOW_MS = 15 * 60 * 1000
export const REVOKE_WINDOW_MS = 48 * 60 * 60 * 1000

const age = (m: MessageView, now: number) => now - Date.parse(m.timestamp_iso)

/** My own plain text, still inside the edit window. */
export function canEdit(m: MessageView, now: number = Date.now()): boolean {
  const plain = !!m.text && !m.media && !m.poll && !m.contact_cards
  return m.from_me && m.actionable && !m.system && !m.deleted && plain && age(m, now) <= EDIT_WINDOW_MS
}

/** One of my messages, still inside the delete-for-everyone window. */
export function canRevoke(m: MessageView, now: number = Date.now()): boolean {
  return m.from_me && m.actionable && !m.system && !m.deleted && age(m, now) <= REVOKE_WINDOW_MS
}

/** Anything with something to send again: text or media, not a poll, a deleted or an imported row. */
export function canForward(m: MessageView): boolean {
  return m.actionable && !m.system && !m.deleted && !m.poll && (!!m.text || !!m.media) && !m.contact_cards
}
