import { useEffect, useImperativeHandle, useLayoutEffect, useRef, useState, type KeyboardEvent, type Ref } from 'react'
import type { MessageView } from '../api-types'
import { Icon } from './Icon'

export const MAX_TEXT = 4096
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
  ref?: Ref<ComposerHandle>
  /** Ask for a one-off draft; the button is absent without it. */
  onDraft?: () => void
  drafting?: boolean
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

/** The message box: a pill, 16 px text, 44 px targets. Attach is a placeholder until a later version; no mic in M1. */
export function Composer({ replyTo, onCancelReply, onSend, ref, onDraft, drafting }: Props) {
  const [text, setText] = useState('')
  const box = useRef<HTMLTextAreaElement>(null)
  const wrap = useRef<HTMLDivElement>(null)
  useKeyboardInset(wrap)

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

  const current = useRef(text)
  useEffect(() => {
    current.current = text
  })
  useImperativeHandle(
    ref,
    () => ({
      getText: () => current.current,
      fill: (value) => {
        current.current = value
        setText(value)
        box.current?.focus()
      },
    }),
    [],
  )

  const tooLong = text.length > MAX_TEXT
  const canSend = text.trim().length > 0 && !tooLong

  function submit() {
    if (!canSend) return
    onSend(text)
    setText('') // synchronous: a second tap or Enter finds an empty box and sends nothing
    box.current?.focus()
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing) return
    if (!enterSends()) return
    event.preventDefault()
    submit()
  }

  return (
    <div className="composer" ref={wrap}>
      {replyTo && (
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
      {tooLong && (
        <p className="toolong" role="alert">
          Message too long ({text.length} / {MAX_TEXT})
        </p>
      )}
      <div className="composer-row">
        <button
          className="iconbtn attach"
          type="button"
          disabled
          title="In a later version"
          aria-label="Attach, in a later version"
        >
          <Icon name="attach" size={22} />
        </button>
        {onDraft && (
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
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
        />
        {canSend && (
          <button className="sendbtn" type="button" aria-label="Send" onClick={submit}>
            <Icon name="send" size={22} />
          </button>
        )}
      </div>
    </div>
  )
}
