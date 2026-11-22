import type { ReactionView } from '../api-types'

export const QUICK_REACTIONS = ['👍', '❤️', '😂', '😮', '😢', '🙏']

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

/** The six quick reactions of the long-press menu. Picking the one you already have takes it back. */
export function ReactionPicker({ mine, onPick }: { mine: string; onPick: (emoji: string) => void }) {
  return (
    <div className="picker" role="group" aria-label="React">
      {QUICK_REACTIONS.map((emoji) => (
        <button
          key={emoji}
          className={emoji === mine ? 'pick on' : 'pick'}
          type="button"
          aria-label={emoji === mine ? `Remove ${emoji}` : `React ${emoji}`}
          aria-pressed={emoji === mine}
          onClick={() => onPick(emoji)}
        >
          {emoji}
        </button>
      ))}
    </div>
  )
}
