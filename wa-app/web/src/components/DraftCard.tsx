import type { DraftView } from '../api-types'
import { Icon } from './Icon'

type Props = {
  draft: DraftView
  /** Who the reply goes to: the chat's name (a group's quote names the sender). */
  name: string
  /** A row was tapped: put this text in the composer. Never sends. */
  onUse: (text: string) => void
  onDismiss: () => void
  onRetry: () => void
}

/** The AI draft above the composer: dashed, amber, labelled, squarer than a bubble. Rows say "Use", never "Send". */
export function DraftCard({ draft, name, onUse, onDismiss, onRetry }: Props) {
  const to = draft.quote?.sender_name || name
  return (
    <section className="draft-card" aria-label="Draft reply">
      <header className="draft-head">
        <Icon name="sparkle" size={16} />
        <span className="draft-label" dir="auto">
          {draft.source === 'agent' ? 'Draft · from Claude' : `Draft · replying to ${to}`}
        </span>
        <button className="iconbtn draft-x" type="button" aria-label="Dismiss draft" onClick={onDismiss}>
          <Icon name="close" size={20} />
        </button>
      </header>
      {draft.quote?.preview && (
        <p className="draft-quote" dir="auto">
          {draft.quote.preview}
        </p>
      )}
      {draft.status === 'pending' && (
        <p className="draft-pending" role="status">
          <span className="draft-shimmer">Drafting…</span>
        </p>
      )}
      {draft.status === 'failed' && (
        <div className="draft-failed" role="alert">
          <span dir="auto">{draft.error || "Couldn't draft"}</span>
          <button className="button-text" type="button" onClick={onRetry}>
            Try again
          </button>
        </div>
      )}
      {draft.status === 'ready' &&
        draft.alternatives.map((text, i) => (
          <button className="draft-alt" type="button" key={`${i}:${text}`} onClick={() => onUse(text)}>
            <span className="draft-alt-text" dir="auto">
              {text}
            </span>
            <span className="draft-use">Use</span>
          </button>
        ))}
    </section>
  )
}
