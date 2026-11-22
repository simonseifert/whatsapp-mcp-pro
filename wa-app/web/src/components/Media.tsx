import { useEffect, useState } from 'react'
import type { MediaView } from '../api-types'
import { mediaUrl } from '../api'
import { formatBytes, formatDuration } from '../format'
import { Icon, type IconName } from './Icon'
import { VoiceNote } from './VoiceNote'

const LABEL: Record<string, [IconName, string]> = {
  image: ['image', 'Photo'],
  video: ['video', 'Video'],
  sticker: ['image', 'Sticker'],
  document: ['file', 'Document'],
  audio: ['mic', 'Voice message'],
  ptt: ['mic', 'Voice message'],
}

type Props = { chat: string; id: string; media: MediaView }

/** Neutral placeholder for media with no file. "Try again" is the only thing that may ask the sender's phone. */
function Missing({ chat, id, media, onFound }: Props & { onFound: () => void }) {
  const [icon, label] = LABEL[media.type] ?? ['file', 'Attachment']
  const voice = media.type === 'audio' || media.type === 'ptt'
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string | null>(null)

  async function retry() {
    setBusy(true)
    setNote(null)
    try {
      const response = await fetch(mediaUrl(chat, id, 'retry'), { credentials: 'same-origin' })
      if (response.ok) return onFound()
      setNote(response.status === 429 ? 'Asked recently, try again later' : 'Still not available')
    } catch {
      setNote('Could not reach the server')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={voice ? 'photo voiceph' : 'photo'}>
      <Icon name={icon} size={26} />
      <span>
        Media not available
        {voice && media.duration_s !== null ? ` · ${label} ${formatDuration(media.duration_s)}` : ''}
      </span>
      <button type="button" className="mretry" onClick={() => void retry()} disabled={busy}>
        {busy ? 'Asking…' : 'Try again'}
      </button>
      {note && (
        <span className="ph-note" role="status">
          {note}
        </span>
      )}
    </div>
  )
}

function Viewer({ src, onClose }: { src: string; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="viewer" role="dialog" aria-modal="true" aria-label="Photo" onClick={onClose}>
      <button type="button" className="viewer-close" onClick={onClose} aria-label="Close photo">
        <Icon name="close" size={24} />
      </button>
      <img src={src} alt="" onClick={(e) => e.stopPropagation()} />
    </div>
  )
}

/** The file part of a message: picture, sticker, video, document or voice note; a placeholder when missing. */
export function Media({ chat, id, media }: Props) {
  const [found, setFound] = useState(false)
  const [broken, setBroken] = useState(false)
  const [viewing, setViewing] = useState(false)
  const available = (media.has_file || found) && !broken
  if (!available) {
    return <Missing chat={chat} id={id} media={media} onFound={() => (setBroken(false), setFound(true))} />
  }
  const voice = media.type === 'audio' || media.type === 'ptt'
  if (voice) return <VoiceNote chat={chat} id={id} durationS={media.duration_s} onBroken={() => setBroken(true)} />
  if (media.type === 'image' || media.type === 'sticker') {
    const label = media.type === 'sticker' ? 'sticker' : 'photo'
    return (
      <>
        <button
          type="button"
          className={`mbtn ${media.type}`}
          onClick={() => setViewing(true)}
          aria-label={`Open ${label}`}
        >
          <img src={mediaUrl(chat, id, 'thumb')} alt="" loading="lazy" onError={() => setBroken(true)} />
        </button>
        {viewing && <Viewer src={mediaUrl(chat, id)} onClose={() => setViewing(false)} />}
      </>
    )
  }
  if (media.type === 'video') {
    return (
      <video
        className="vid"
        controls
        playsInline
        preload="none"
        poster={mediaUrl(chat, id, 'thumb')}
        src={mediaUrl(chat, id)}
        onError={() => setBroken(true)}
      />
    )
  }
  return (
    <a className="docrow" href={mediaUrl(chat, id)} download={media.filename ?? undefined}>
      <Icon name="file" size={26} />
      <span className="docname" dir="auto">
        {media.filename ?? 'Document'}
      </span>
      {media.size_bytes !== null && <span className="docsize">{formatBytes(media.size_bytes)}</span>}
    </a>
  )
}
