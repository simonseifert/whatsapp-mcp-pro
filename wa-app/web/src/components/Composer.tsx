import { useEffect, useImperativeHandle, useLayoutEffect, useRef, useState, type KeyboardEvent, type Ref } from 'react'
import { fetchQuickReplies, markQuickReplyUsed } from '../api'
import type { MessageView, QuickReply } from '../api-types'
import { Icon } from './Icon'
import { filterReplies, QuickReplyPicker } from './QuickReplyPicker'
import { RecordBar } from './RecordBar'
import { canRecord, useRecorder, voiceFilename } from '../useRecorder'

export const MAX_TEXT = 4096
const MIC_SEEN = 'wa-mic-explained'
export const MIC_EXPLAINER = 'wa-app needs the microphone to record voice messages.'
const MAX_ROWS_PX = 132 // about five lines, then the box scrolls

/** What the chat screen may do to the box from outside: read it and fill it (a draft's "Use"). */
export type ComposerHandle = {
  getText: () => string
  fill: (text: string) => void
}

type Props = {
  replyTo: MessageView | null
  onCancelReply: () => void
  onSend: (text: string) => void
  /** A file was picked with the paperclip (photos, camera and any file). */
  onAttach?: (file: File) => void
  /** A voice note was recorded and Send was tapped. Nothing is uploaded before that. */
  onVoice?: (file: File) => void
  ref?: Ref<ComposerHandle>
  /** Ask for a one-off draft; the button is absent without it. */
  onDraft?: () => void
  /** The box's text changed (typing, clearing, a draft filled in, or a send emptied it). */
  onTextChange?: (text: string) => void
  drafting?: boolean
  /** Edit mode: the box holds this message's text and Send saves it instead of sending a new message. */
  editing?: MessageView | null
  onSaveEdit?: (text: string) => void
  onCancelEdit?: () => void
}

const quoteLabel = (m: MessageView) => m.text?.trim() || (m.media ? `[${m.media.type}]` : m.poll ? m.poll.question : '')

/** True where Enter should send (a physical keyboard); on touch screens Enter makes a new line. */
const enterSends = () => window.matchMedia?.('(hover: hover) and (pointer: fine)').matches ?? false

/** Keeps the composer above the on-screen keyboard: the visual viewport shrinks, the layout viewport does not. */
function useKeyboardInset(target: React.RefObject<HTMLElement | null>) {
  useEffect(() => {
    const vv = window.visualViewport
    const el = target.current
    if (!vv || !el) return
    const apply = () => {
      const inset = Math.max(0, window.innerHeight - vv.height - vv.offsetTop)
      el.style.setProperty('--kb-inset', `${Math.round(inset)}px`)
    }
    apply()
    vv.addEventListener('resize', apply)
    vv.addEventListener('scroll', apply)
    return () => {
      vv.removeEventListener('resize', apply)
      vv.removeEventListener('scroll', apply)
      el.style.removeProperty('--kb-inset')
    }
  }, [target])
}

/** The message box: a pill, 16 px text, 44 px targets. The paperclip opens the system file picker (on a phone: camera, photos, files). */
export function Composer({
  replyTo,
  onCancelReply,
  onSend,
  onAttach,
  onVoice,
  ref,
  onDraft,
  drafting,
  onTextChange,
  editing = null,
  onSaveEdit,
  onCancelEdit,
}: Props) {
  const [text, setText] = useState('')
  const current = useRef(text)
  useEffect(() => {
    current.current = text
  })
  const stash = useRef('') // what was half typed when edit mode began
  const box = useRef<HTMLTextAreaElement>(null)
  const wrap = useRef<HTMLDivElement>(null)
  const picker = useRef<HTMLInputElement>(null)
  useKeyboardInset(wrap)
  const rec = useRecorder()
  const recording = rec.state !== 'idle'
  const [explain, setExplain] = useState(false)

  // The first time, say why before the browser asks for the microphone.
  function onMic() {
    let seen = true
    try {
      seen = localStorage.getItem(MIC_SEEN) === '1'
      if (!seen) localStorage.setItem(MIC_SEEN, '1')
    } catch {
      // Storage is blocked: skip the note rather than ask every time.
    }
    if (seen) void rec.start()
    else setExplain(true)
  }

  useLayoutEffect(() => {
    const el = box.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, MAX_ROWS_PX)}px`
  }, [text])

  // Replying focuses the box so the answer can be typed straight away.
  useEffect(() => {
    if (replyTo) box.current?.focus()
  }, [replyTo])

  // Entering edit mode loads the message's text; leaving it (saved or cancelled) empties the box again.
  const editId = editing?.id ?? null
  const editText = editing?.text ?? ''
  const wasEditing = useRef(false)
  useEffect(() => {
    if (editId !== null) {
      if (!wasEditing.current) stash.current = current.current
      wasEditing.current = true
      // eslint-disable-next-line react-hooks/set-state-in-effect -- the box takes the edited message's text once
      setText(editText)
      box.current?.focus()
    } else if (wasEditing.current) {
      wasEditing.current = false
      setText(stash.current)
      stash.current = ''
    }
  }, [editId, editText])

  // Typing "/" at the very start opens the quick reply picker (never while editing a sent message).
  const [replies, setReplies] = useState<QuickReply[] | null>(null)
  const [pickerOff, setPickerOff] = useState(false)
  const slash = !editing && /^\/[^\s]*$/.test(text)
  const pickerOpen = slash && !pickerOff
  useEffect(() => {
    if (!pickerOpen || replies) return
    let live = true
    fetchQuickReplies()
      .then((r) => live && setReplies(r))
      .catch(() => undefined) // leave it unloaded: the next open asks again
    return () => {
      live = false
    }
  }, [pickerOpen, replies])
  const matches = pickerOpen && replies ? filterReplies(replies, text.slice(1)) : []

  function pick(reply: QuickReply) {
    setPickerOff(false)
    setText(reply.text)
    current.current = reply.text
    onTextChange?.(reply.text)
    box.current?.focus()
    setReplies((all) => all && all.map((r) => (r.shortcut === reply.shortcut ? { ...r, used: r.used + 1 } : r)))
    void markQuickReplyUsed(reply.shortcut).catch(() => undefined) // the count is a nicety; the text is already in the box
  }

  useImperativeHandle(
    ref,
    () => ({
      getText: () => current.current,
      fill: (value) => {
        current.current = value
        setPickerOff(false)
        setText(value)
        onTextChange?.(value)
        box.current?.focus()
      },
    }),
    [onTextChange],
  )

  const tooLong = text.length > MAX_TEXT
  const canSend = text.trim().length > 0 && !tooLong
  const isEditing = editing !== null
  const canSave = canSend && text.trim() !== (editing?.text ?? '').trim()

  function submit() {
    if (isEditing) {
      if (canSave) onSaveEdit?.(text.trim())
      return
    }
    if (!canSend) return
    onSend(text)
    setPickerOff(false)
    onTextChange?.('')
    setText('') // synchronous: a second tap or Enter finds an empty box and sends nothing
    box.current?.focus()
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Escape' && pickerOpen) {
      event.preventDefault()
      setPickerOff(true)
      return
    }
    if (event.key === 'Escape' && isEditing) {
      event.preventDefault()
      onCancelEdit?.()
      return
    }
    if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing) return
    if (!enterSends()) return
    event.preventDefault()
    if (pickerOpen && replies === null) return // still loading: never send a bare "/"
    if (pickerOpen && matches.length > 0) pick(matches[0])
    else submit()
  }

  return (
    <div className="composer" ref={wrap} data-editing={isEditing ? 'true' : 'false'}>
      {isEditing && (
        <div className="replybar">
          <div className="replytxt">
            <b>Editing message</b>
            <span dir="auto">{editing.text}</span>
          </div>
          <button className="iconbtn" type="button" aria-label="Cancel edit" onClick={onCancelEdit}>
            <Icon name="close" size={20} />
          </button>
        </div>
      )}
      {replyTo && !isEditing && (
        <div className="replybar">
          <div className="replytxt">
            <b dir="auto">{replyTo.from_me ? 'You' : (replyTo.sender_name ?? 'Reply')}</b>
            <span dir="auto">{quoteLabel(replyTo)}</span>
          </div>
          <button className="iconbtn" type="button" aria-label="Cancel reply" onClick={onCancelReply}>
            <Icon name="close" size={20} />
          </button>
        </div>
      )}
      {pickerOpen && replies && <QuickReplyPicker replies={matches} onPick={pick} />}
      {tooLong && (
        <p className="toolong" role="alert">
          Message too long ({text.length} / {MAX_TEXT})
        </p>
      )}
      {explain && !recording && (
        <div className="micnote">
          <p>{MIC_EXPLAINER}</p>
          <button
            type="button"
            className="micnote-go"
            onClick={() => {
              setExplain(false)
              void rec.start()
            }}
          >
            Continue
          </button>
        </div>
      )}
      {rec.state === 'idle' && rec.message && (
        <p className="toolong" role="alert">
          {rec.message}
        </p>
      )}
      {recording && !isEditing ? (
        <RecordBar
          state={rec.state}
          seconds={rec.seconds}
          blob={rec.blob}
          message={rec.message}
          onStop={rec.stop}
          onCancel={rec.cancel}
          onSend={(blob) => {
            const file = new File([blob], voiceFilename(blob), { type: blob.type })
            rec.cancel()
            onVoice?.(file)
          }}
        />
      ) : (
      <div className="composer-row">
        <input
          ref={picker}
          type="file"
          hidden
          tabIndex={-1}
          aria-hidden="true"
          data-testid="file-input"
          onChange={(e) => {
            const file = e.target.files?.[0]
            e.target.value = '' // the same file can be picked again
            if (file) onAttach?.(file)
          }}
        />
        {!isEditing && (
        <button
          className="iconbtn attach"
          type="button"
          disabled={!onAttach}
          aria-label="Attach a photo or file"
          title="Attach a photo or file"
          onClick={() => picker.current?.click()}
        >
          <Icon name="attach" size={22} />
        </button>
        )}
        {onDraft && !isEditing && (
          <button
            className="iconbtn draftbtn"
            type="button"
            aria-label="Draft a reply"
            title="Draft a reply"
            disabled={drafting}
            onClick={onDraft}
          >
            <Icon name="sparkle" size={22} />
          </button>
        )}
        <textarea
          ref={box}
          className="composer-input"
          rows={1}
          value={text}
          placeholder="Message"
          aria-label="Message"
          enterKeyHint="enter"
          onChange={(e) => {
            setText(e.target.value)
            if (!e.target.value.startsWith('/')) setPickerOff(false) // a new "/" opens it again after Escape
            onTextChange?.(e.target.value)
          }}
          onKeyDown={onKeyDown}
        />
        {isEditing ? (
          <button className="sendbtn" type="button" aria-label="Save edit" disabled={!canSave} onClick={submit}>
            <Icon name="check" size={22} />
          </button>
        ) : canSend ? (
          <button className="sendbtn" type="button" aria-label="Send" onClick={submit}>
            <Icon name="send" size={22} />
          </button>
        ) : (
          onVoice &&
          text.length === 0 &&
          canRecord() && (
            <button
              className="sendbtn"
              type="button"
              aria-label="Record voice message"
              title="Record voice message"
              onClick={onMic}
            >
              <Icon name="mic" size={22} />
            </button>
          )
        )}
      </div>
      )}
    </div>
  )
}
