import type { MessageView } from '../api-types'
import { formatClock, formatDuration } from '../format'
import { ContactCard } from './ContactCard'
import { Icon, type IconName } from './Icon'
import { Poll } from './Poll'
import { ReactionBar } from './ReactionBar'
import { RichText } from './RichText'

const MEDIA_LABEL: Record<string, [IconName, string]> = {
  image: ['image', 'Photo'],
  video: ['video', 'Video'],
  sticker: ['image', 'Sticker'],
  document: ['file', 'Document'],
  audio: ['mic', 'Voice message'],
  ptt: ['mic', 'Voice message'],
}

function Media({ message }: { message: NonNullable<MessageView['media']> }) {
  const [icon, label] = MEDIA_LABEL[message.type] ?? ['file', 'Attachment']
  const voice = message.type === 'audio' || message.type === 'ptt'
  const detail = voice
    ? message.duration_s !== null
      ? formatDuration(message.duration_s)
      : null
    : message.type === 'document'
      ? message.filename
      : null
  // The file itself (and its player or thumbnail) arrives with the media step; until then, and for media that
  // was never downloaded, this is a labelled placeholder rather than a broken image.
  return (
    <div className={voice ? 'photo voiceph' : 'photo'}>
      <Icon name={icon} size={26} />
      <span>{detail ? `${label} · ${detail}` : label}</span>
      {!message.has_file && <span className="ph-note">Not downloaded</span>}
    </div>
  )
}

type Props = { message: MessageView; showSender: boolean; tail: boolean }

/** One message: bubble (tail on the last of a run), and its reactions beneath. */
export function Bubble({ message: m, showSender, tail }: Props) {
  const side = m.from_me ? 'me' : 'them'
  const hasMedia = m.media !== null
  const classes = ['bubble', m.deleted ? 'deleted' : '', hasMedia ? 'media' : ''].filter(Boolean).join(' ')
  return (
    <div className={`msg ${side}${tail ? ' tail' : ' run'}`} data-message-id={m.id}>
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
            {m.media && <Media message={m.media} />}
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
