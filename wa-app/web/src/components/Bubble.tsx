import type { MessageView } from '../api-types'
import { formatClock } from '../format'
import { ContactCard } from './ContactCard'
import { Icon } from './Icon'
import { Media } from './Media'
import { Poll } from './Poll'
import { ReactionBar } from './ReactionBar'
import { RichText } from './RichText'

type Props = { message: MessageView; showSender: boolean; tail: boolean; rise?: boolean }

/** One message: bubble (tail on the last of a run), and its reactions beneath. */
export function Bubble({ message: m, showSender, tail, rise = false }: Props) {
  const side = m.from_me ? 'me' : 'them'
  const hasMedia = m.media !== null
  const classes = ['bubble', m.deleted ? 'deleted' : '', hasMedia ? 'media' : ''].filter(Boolean).join(' ')
  return (
    <div className={`msg ${side}${tail ? ' tail' : ' run'}${rise ? ' rise' : ''}`} data-message-id={m.id}>
      <div className={classes}>
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
            {m.contact_cards?.map((card, i) => <ContactCard key={i} card={card} />)}
            {m.text && <RichText text={m.text} />}
          </>
        )}
        <div className="meta">
          {m.edited && <span>Edited</span>}
          <time dateTime={m.timestamp_iso}>{formatClock(m.timestamp_iso)}</time>
        </div>
      </div>
      {!m.deleted && <ReactionBar reactions={m.reactions} />}
    </div>
  )
}
