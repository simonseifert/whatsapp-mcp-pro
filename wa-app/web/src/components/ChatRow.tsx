import { useRef, useState } from 'react'
import { Link } from 'react-router'
import type { ChatSummary } from '../api-types'
import { formatListTime } from '../format'
import { useChatState } from '../useChatState'
import { Avatar } from './Avatar'
import { ChatMenu } from './ChatMenu'
import { Icon } from './Icon'
import { Ticks } from './Ticks'

const GROUP_KINDS = ['group', 'community_group', 'announcement']

function unreadLabel(chat: ChatSummary): string | null {
  if (chat.unread > 0) return `${chat.unread} unread`
  return chat.marked_unread ? 'Marked unread' : null
}

const LONG_PRESS_MS = 500
const MOVE_SLOP_PX = 10

export function ChatRow({ chat: listed, now }: { chat: ChatSummary; now?: Date }) {
  const { chat, error, change, dismissError } = useChatState(listed)
  const [menuOpen, setMenuOpen] = useState(false)
  const press = useRef<{ timer: number; x: number; y: number } | null>(null)
  const pressed = useRef(false) // a long press just opened the menu: the click that follows must not open the chat
  const cancelPress = () => {
    if (press.current) window.clearTimeout(press.current.timer)
    press.current = null
  }
  const last = chat.last_message
  const unread = chat.unread > 0 || chat.marked_unread
  const inGroup = GROUP_KINDS.includes(chat.kind)
  const prefix = last.from_me ? (inGroup ? 'You' : null) : inGroup ? last.sender_name : null
  const label = unreadLabel(chat)
  return (
    <li>
      <Link
        className={unread ? 'row unread' : 'row'}
        to={`/chat/${encodeURIComponent(chat.jid)}`}
        onContextMenu={(e) => {
          e.preventDefault()
          setMenuOpen(true)
        }}
        onTouchStart={(e) => {
          const touch = e.touches[0]
          cancelPress()
          press.current = {
            x: touch?.clientX ?? 0,
            y: touch?.clientY ?? 0,
            timer: window.setTimeout(() => {
              press.current = null
              pressed.current = true
              setMenuOpen(true)
            }, LONG_PRESS_MS),
          }
        }}
        onTouchMove={(e) => {
          const touch = e.touches[0]
          if (press.current && touch && Math.hypot(touch.clientX - press.current.x, touch.clientY - press.current.y) > MOVE_SLOP_PX)
            cancelPress()
        }}
        onTouchEnd={cancelPress}
        onTouchCancel={cancelPress}
        onClick={(e) => {
          if (pressed.current) {
            e.preventDefault()
            pressed.current = false
          }
        }}
      >
        <Avatar name={chat.name} src={chat.avatar_url} kind={chat.kind} />
        <div className="body">
          <div className="top">
            <span className="name">{chat.name}</span>
            <time className="time" dateTime={last.timestamp_iso}>
              {formatListTime(last.timestamp_iso, now)}
            </time>
          </div>
          <div className="bot">
            <div className={last.deleted ? 'prev del' : 'prev'}>
              {last.deleted && <Icon name="ban" size={15} />}
              {last.from_me && !last.deleted && <Ticks state={last.ticks} />}
              {prefix && <b>{prefix}: </b>}
              {last.preview}
            </div>
            <div className="badges">
              {chat.muted && <Icon name="bellOff" size={16} label="Muted" />}
              {chat.pinned && <Icon name="pin" size={16} label="Pinned" />}
              {chat.unread > 0 && (
                <span className={chat.muted ? 'badge muted' : 'badge'} aria-label={label ?? undefined}>
                  {chat.unread > 99 ? '99+' : chat.unread}
                </span>
              )}
              {chat.unread === 0 && chat.marked_unread && <span className="dot" role="img" aria-label={label ?? undefined} />}
            </div>
          </div>
        </div>
      </Link>
      {error && (
        <p className="row-error" role="alert">
          {error}{' '}
          <button className="button-text" type="button" onClick={dismissError}>
            Dismiss
          </button>
        </p>
      )}
      {menuOpen && (
        <ChatMenu
          state={chat}
          onChange={(patch) => {
            setMenuOpen(false)
            void change(patch)
          }}
          onClose={() => setMenuOpen(false)}
        />
      )}
    </li>
  )
}
