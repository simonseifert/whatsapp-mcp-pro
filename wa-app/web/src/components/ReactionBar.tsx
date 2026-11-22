import type { ReactionView } from '../api-types'

/** The emoji chips under a bubble: one per emoji with its count; yours is outlined. */
export function ReactionBar({ reactions }: { reactions: ReactionView[] }) {
  if (reactions.length === 0) return null
  return (
    <div className="react">
      {reactions.map((r) => (
        <span
          key={r.emoji}
          className={r.mine ? 'rx mine' : 'rx'}
          aria-label={`${r.count} ${r.emoji}${r.mine ? ', including you' : ''}`}
        >
          <span aria-hidden="true">{r.emoji}</span>
          <span aria-hidden="true">{r.count}</span>
        </span>
      ))}
    </div>
  )
}
