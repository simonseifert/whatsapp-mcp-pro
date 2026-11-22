import { useEffect, useRef } from 'react'
import type { MessageView } from '../api-types'
import { Icon } from './Icon'
import { ReactionPicker } from './ReactionBar'

type Props = {
  message: MessageView
  mine: string
  onReact: (emoji: string) => void
  onReply: () => void
  onCopy: () => void
  onClose: () => void
}

/** Long press (or right click) on a bubble: quick reactions, Reply, Copy. Imported rows get Copy only. */
export function MessageMenu({ message: m, mine, onReact, onReply, onCopy, onClose }: Props) {
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
        {m.text && (
          <button className="menu-item" type="button" onClick={onCopy}>
            <Icon name="copy" size={20} />
            Copy
          </button>
        )}
      </div>
    </div>
  )
}
