import { Link } from 'react-router'
import type { ChatSummary } from '../api-types'
import { formatListTime } from '../format'
import { Avatar } from './Avatar'
import { Icon } from './Icon'

const GROUP_KINDS = ['group', 'community_group', 'announcement']

function unreadLabel(chat: ChatSummary): string | null {
  if (chat.unread > 0) return `${chat.unread} unread`
  return chat.marked_unread ? 'Marked unread' : null
}

export function ChatRow({ chat, now }: { chat: ChatSummary; now?: Date }) {
  const last = chat.last_message
  const unread = chat.unread > 0 || chat.marked_unread
  const inGroup = GROUP_KINDS.includes(chat.kind)
  const prefix = last.from_me ? (inGroup ? 'You' : null) : inGroup ? last.sender_name : null
  const label = unreadLabel(chat)
  return (
    <li>
      <Link className={unread ? 'row unread' : 'row'} to={`/chat/${encodeURIComponent(chat.jid)}`}>
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
              {prefix && <b>{prefix}: </b>}
              {last.preview}
            </div>
            <div className="badges">
              {chat.muted && <Icon name="mute" size={16} />}
              {chat.pinned && <Icon name="pin" size={16} />}
              {chat.unread > 0 && (
                <span className={chat.muted ? 'badge muted' : 'badge'} aria-label={label ?? undefined}>
                  {chat.unread}
                </span>
              )}
              {chat.unread === 0 && chat.marked_unread && <span className="dot" role="img" aria-label={label ?? undefined} />}
            </div>
          </div>
        </div>
      </Link>
    </li>
  )
}
