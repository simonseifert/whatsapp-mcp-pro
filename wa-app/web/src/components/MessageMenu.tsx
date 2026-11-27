import { useEffect, useRef, useState } from 'react'
import type { MessageView } from '../api-types'
import { canEdit, canForward, canRevoke } from '../messageActions'
import { Icon } from './Icon'
import { ReactionPicker } from './ReactionBar'

type Props = {
  message: MessageView
  mine: string
  onReact: (emoji: string) => void
  onReply: () => void
  /** Only for another person's message in a group. */
  onReplyPrivately?: () => void
  onCopy: () => void
  onClose: () => void
  /** Each action shows only when its handler is given and the message allows it (time limits included). */
  onEdit?: () => void
  onRevoke?: () => void
  onStar?: () => void
  onForward?: () => void
  /** The clock for the time limits; tests pin it. */
  now?: number
}

/** Long press (or right click) on a bubble: reactions, Reply, Reply privately (groups), Copy, Edit and Delete for everyone
 *  (own messages, inside WhatsApp's time limits), Star, Forward. Imported rows get Copy only. */
export function MessageMenu({
  message: m,
  mine,
  onReact,
  onReply,
  onReplyPrivately,
  onCopy,
  onClose,
  onEdit,
  onRevoke,
  onStar,
  onForward,
  now: clock,
}: Props) {
  const [opened] = useState(() => Date.now()) // the menu's own moment: fixed while it is open
  const now = clock ?? opened
  const first = useRef<HTMLDivElement>(null)
  useEffect(() => {
    first.current?.querySelector<HTMLElement>('button')?.focus()
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="menu-wrap">
      <button className="menu-backdrop" type="button" aria-label="Close menu" onClick={onClose} tabIndex={-1} />
      <div className="menu" role="dialog" aria-label="Message actions" ref={first}>
        {m.actionable && <ReactionPicker mine={mine} onPick={onReact} />}
        {m.actionable && (
          <button className="menu-item" type="button" onClick={onReply}>
            <Icon name="reply" size={20} />
            Reply
          </button>
        )}
        {m.actionable && onReplyPrivately && (
          <button className="menu-item" type="button" onClick={onReplyPrivately}>
            <Icon name="reply" size={20} />
            Reply privately
          </button>
        )}
        {m.text && (
          <button className="menu-item" type="button" onClick={onCopy}>
            <Icon name="copy" size={20} />
            Copy
          </button>
        )}
        {m.actionable && !m.system && !m.deleted && onStar && (
          <button className="menu-item" type="button" onClick={onStar}>
            <Icon name="star" size={20} />
            {m.starred ? 'Unstar' : 'Star'}
          </button>
        )}
        {canForward(m) && onForward && (
          <button className="menu-item" type="button" onClick={onForward}>
            <Icon name="forward" size={20} />
            Forward
          </button>
        )}
        {canEdit(m, now) && onEdit && (
          <button className="menu-item" type="button" onClick={onEdit}>
            <Icon name="edit" size={20} />
            Edit
          </button>
        )}
        {canRevoke(m, now) && onRevoke && (
          <button className="menu-item danger" type="button" onClick={onRevoke}>
            <Icon name="trash" size={20} />
            Delete for everyone
          </button>
        )}
      </div>
    </div>
  )
}
