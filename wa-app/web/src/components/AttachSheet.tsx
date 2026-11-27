import { useEffect, useMemo, useRef, useState } from 'react'
import { MAX_UPLOAD } from '../api'
import type { QuoteView } from '../api-types'
import { formatBytes } from '../format'
import { Icon } from './Icon'
import { useDialog } from './useDialog'

export const MAX_CAPTION = 1024
export const TOO_BIG = 'Too big to send (max 64 MB)'

type Props = {
  file: File
  /** The message being answered, if any: its id goes out with the file. */
  replyTo: QuoteView | null
  onSend: (file: File, caption: string, replyTo: QuoteView | null) => void
  onClose: () => void
}

/** What was picked: a preview for a photo, a name and size for anything else, a caption, Send. */
export function AttachSheet({ file, replyTo, onSend, onClose }: Props) {
  const [caption, setCaption] = useState('')
  const dialog = useRef<HTMLDivElement>(null)
  useDialog(dialog, onClose)

  const isImage = file.type.startsWith('image/')
  const preview = useMemo(
    () => (isImage && typeof URL.createObjectURL === 'function' ? URL.createObjectURL(file) : null),
    [file, isImage],
  )
  useEffect(() => () => (preview ? URL.revokeObjectURL(preview) : undefined), [preview])

  const tooBig = file.size > MAX_UPLOAD
  const empty = file.size === 0

  return (
    <div className="menu-wrap sheet-wrap">
      <button className="menu-backdrop" type="button" aria-label="Close" onClick={onClose} tabIndex={-1} />
      <div className="sheet" role="dialog" aria-modal="true" aria-label="Send a file" ref={dialog}>
        <header className="sheet-head">
          <h2>Send {isImage ? 'a photo' : 'a file'}</h2>
          <button className="iconbtn" type="button" aria-label="Close" onClick={onClose}>
            <Icon name="close" size={20} />
          </button>
        </header>
        {isImage && preview ? (
          <img className="attach-preview" src={preview} alt={file.name} />
        ) : (
          <div className="attach-file">
            <Icon name="attach" size={22} />
            <span className="attach-name" dir="auto">
              {file.name}
            </span>
            <span className="attach-size">{formatBytes(file.size)}</span>
          </div>
        )}
        {tooBig && (
          <p className="sheet-warning" role="alert">
            <Icon name="ban" size={18} />
            <span>{TOO_BIG}</span>
          </p>
        )}
        {empty && (
          <p className="sheet-warning" role="alert">
            <Icon name="ban" size={18} />
            <span>This file is empty</span>
          </p>
        )}
        <label className="sheet-field">
          <span>Caption</span>
          <textarea
            rows={2}
            maxLength={MAX_CAPTION}
            value={caption}
            placeholder="Add a caption"
            onChange={(e) => setCaption(e.target.value)}
          />
        </label>
        <button
          className="sheet-save"
          type="button"
          disabled={tooBig || empty}
          onClick={() => onSend(file, caption.trim(), replyTo)}
        >
          Send
        </button>
      </div>
    </div>
  )
}
