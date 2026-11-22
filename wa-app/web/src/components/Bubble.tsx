import { useRef, useState, type PointerEvent } from 'react'
import type { MessageView } from '../api-types'
import { formatClock } from '../format'
import { ContactCard } from './ContactCard'
import { Icon } from './Icon'
import { Media } from './Media'
import { Poll } from './Poll'
import { ReactionBar } from './ReactionBar'
import { RichText } from './RichText'

/** An outgoing text this device is still sending, or could not send. */
export type Delivery = { state: 'sending' | 'sent' | 'failed'; error?: string }

type Props = {
  message: MessageView
  showSender: boolean
  tail: boolean
  rise?: boolean
  delivery?: Delivery
  onRetry?: () => void
  onDismiss?: () => void
  /** Swipe right or the hover button. Absent where a reply is not possible. */
  onReply?: () => void
  /** Long press or right click. */
  onMenu?: () => void
}

const LONG_PRESS_MS = 450
const SWIPE_PX = 56
const SLOP_PX = 8

/** Swipe right to reply, long press to open the menu: touch only, so mouse selection and scrolling are untouched. */
function useGestures(onReply?: () => void, onMenu?: () => void) {
  const [offset, setOffset] = useState(0)
  const start = useRef<{ x: number; y: number } | null>(null)
  const timer = useRef<number | undefined>(undefined)
  const reset = () => {
    window.clearTimeout(timer.current)
    start.current = null
    setOffset(0)
  }
  const handlers = {
    onPointerDown(e: PointerEvent) {
      if (e.pointerType === 'mouse') return
      start.current = { x: e.clientX, y: e.clientY }
      if (onMenu)
        timer.current = window.setTimeout(() => {
          start.current = null
          onMenu()
        }, LONG_PRESS_MS)
    },
    onPointerMove(e: PointerEvent) {
      const s = start.current
      if (!s) return
      const dx = e.clientX - s.x
      const dy = e.clientY - s.y
      if (Math.abs(dx) > SLOP_PX || Math.abs(dy) > SLOP_PX) window.clearTimeout(timer.current)
      if (onReply && dx > 0 && dx > Math.abs(dy)) setOffset(Math.min(dx, 72) * 0.7)
    },
    onPointerUp(e: PointerEvent) {
      const s = start.current
      const swiped = s && onReply && e.clientX - s.x >= SWIPE_PX && Math.abs(e.clientY - s.y) < SWIPE_PX / 2
      reset()
      if (swiped) onReply()
    },
    onPointerCancel: reset,
    onContextMenu(e: React.MouseEvent) {
      if (!onMenu) return
      e.preventDefault()
      onMenu()
    },
  }
  return { handlers, offset }
}

/** One message: bubble (tail on the last of a run), and its reactions beneath. */
export function Bubble({
  message: m,
  showSender,
  tail,
  rise = false,
  delivery,
  onRetry,
  onDismiss,
  onReply,
  onMenu,
}: Props) {
  const side = m.from_me ? 'me' : 'them'
  const gestures = useGestures(onReply, onMenu)
  const hasMedia = m.media !== null
  const classes = ['bubble', m.deleted ? 'deleted' : '', hasMedia ? 'media' : ''].filter(Boolean).join(' ')
  return (
    <div
      className={`msg ${side}${tail ? ' tail' : ' run'}${rise ? ' rise' : ''}${delivery ? ` ${delivery.state}` : ''}`}
      data-message-id={m.id}
      {...gestures.handlers}
    >
      <div className={classes} style={gestures.offset ? { transform: `translateX(${gestures.offset}px)` } : undefined}>
        {showSender && m.sender_name && (
          <div className="sender" dir="auto">
            {m.sender_name}
          </div>
        )}
        {m.deleted ? (
          <div className="del">
            <Icon name="ban" size={15} />
            Message deleted
          </div>
        ) : (
          <>
            {m.forwarded && (
              <div className="fwd">
                <Icon name="forward" size={14} />
                Forwarded
              </div>
            )}
            {m.reply_to && (
              <div className="quote">
                {m.reply_to.sender_name && <b dir="auto">{m.reply_to.sender_name}</b>}
                <span dir="auto">{m.reply_to.preview}</span>
              </div>
            )}
            {m.media && <Media chat={m.chat_jid} id={m.id} media={m.media} />}
            {m.poll && <Poll poll={m.poll} />}
            {m.contact_cards?.map((card, i) => (
              <ContactCard key={i} card={card} />
            ))}
            {m.text && <RichText text={m.text} />}
          </>
        )}
        <div className="meta">
          {m.edited && <span>Edited</span>}
          {delivery?.state === 'sending' ? (
            <span>Sending…</span>
          ) : delivery?.state === 'failed' ? (
            <span>Not sent</span>
          ) : (
            <time dateTime={m.timestamp_iso}>{formatClock(m.timestamp_iso)}</time>
          )}
        </div>
      </div>
      {onReply && (
        <button className="reply-hover" type="button" aria-label="Reply" onClick={onReply}>
          <Icon name="reply" size={18} />
        </button>
      )}
      {delivery?.state === 'failed' && (
        <div className="failline" role="alert">
          <span dir="auto">{delivery.error}</span>
          <button className="button-text" type="button" onClick={onRetry}>
            Retry
          </button>
          <button className="button-text quiet" type="button" onClick={onDismiss}>
            Discard
          </button>
        </div>
      )}
      {!m.deleted && <ReactionBar reactions={m.reactions} />}
    </div>
  )
}
