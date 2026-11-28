import type { QuickReply } from '../api-types'

/** Replies whose shortcut starts with what was typed after the slash. Order (used, then shortcut) is kept. */
export function filterReplies(replies: QuickReply[], query: string): QuickReply[] {
  const q = query.toLowerCase()
  return replies.filter((r) => r.shortcut.startsWith(q))
}

/** The list above the composer after typing "/". Choosing a reply puts its text in the box; it never sends. */
export function QuickReplyPicker({ replies, onPick }: { replies: QuickReply[]; onPick: (reply: QuickReply) => void }) {
  if (replies.length === 0) return <p className="qr-empty">No quick replies match</p>
  return (
    <ul className="qr-picker" role="listbox" aria-label="Quick replies">
      {replies.map((r) => (
        <li key={r.shortcut} role="option" aria-selected={false} onMouseDown={(e) => e.preventDefault()} onClick={() => onPick(r)}>
          <b>/{r.shortcut}</b>
          <span dir="auto">{r.text}</span>
        </li>
      ))}
    </ul>
  )
}
