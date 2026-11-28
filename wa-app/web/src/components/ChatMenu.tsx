import { useRef, useState } from 'react'
import type { ChatStatePatch } from '../api'
import { Icon } from './Icon'
import { useDialog } from './useDialog'

export type ChatMenuState = { pinned: boolean; muted: boolean; archived: boolean; marked_unread: boolean }

const MUTE_CHOICES = [
  { label: '8 hours', value: '8h' },
  { label: '1 week', value: '1w' },
  { label: 'Always', value: 'forever' },
] as const

/** The chat menu (header kebab, or a long press on a list row): pin, mute, archive, mark unread, and in a chat search, lists and AI drafts. */
export function ChatMenu({
  state,
  onChange,
  onAi,
  onSearch,
  onLists,
  onClose,
}: {
  state?: ChatMenuState
  onChange?: (patch: ChatStatePatch) => void
  onAi?: () => void
  onSearch?: () => void
  onLists?: () => void
  onClose: () => void
}) {
  const dialog = useRef<HTMLDivElement>(null)
  const [muting, setMuting] = useState(false)
  useDialog(dialog, onClose)
  const change = (patch: ChatStatePatch) => () => onChange?.(patch)
  return (
    <div className="menu-wrap menu-wrap-top-right">
      <button className="menu-backdrop" type="button" aria-label="Close menu" onClick={onClose} tabIndex={-1} />
      <div className="menu" role="dialog" aria-modal="true" aria-label="Chat actions" ref={dialog}>
        {muting ? (
          <>
            {MUTE_CHOICES.map((choice) => (
              <button key={choice.value} className="menu-item" type="button" onClick={change({ mute: choice.value })}>
                {choice.label}
              </button>
            ))}
            <button className="menu-item" type="button" onClick={() => setMuting(false)}>
              <Icon name="back" size={20} />
              Back
            </button>
          </>
        ) : (
          <>
            {onAi && (
              <button className="menu-item" type="button" onClick={onAi}>
                <Icon name="sparkle" size={20} />
                AI drafts
              </button>
            )}
            {onSearch && (
              <button className="menu-item" type="button" onClick={onSearch}>
                <Icon name="search" size={20} />
                Search
              </button>
            )}
            {onLists && (
              <button className="menu-item" type="button" onClick={onLists}>
                <Icon name="list" size={20} />
                Add to list
              </button>
            )}
            {state && onChange && (
              <>
                <button className="menu-item" type="button" onClick={change({ pin: !state.pinned })}>
                  <Icon name="pin" size={20} />
                  {state.pinned ? 'Unpin chat' : 'Pin chat'}
                </button>
                {state.muted ? (
                  <button className="menu-item" type="button" onClick={change({ mute: false })}>
                    <Icon name="bellOff" size={20} />
                    Unmute notifications
                  </button>
                ) : (
                  <button className="menu-item" type="button" onClick={() => setMuting(true)}>
                    <Icon name="bellOff" size={20} />
                    Mute notifications
                  </button>
                )}
                <button className="menu-item" type="button" onClick={change({ archive: !state.archived })}>
                  <Icon name="archive" size={20} />
                  {state.archived ? 'Unarchive chat' : 'Archive chat'}
                </button>
                <button className="menu-item" type="button" onClick={change({ unread: !state.marked_unread })}>
                  <Icon name="chats" size={20} />
                  {state.marked_unread ? 'Mark as read' : 'Mark as unread'}
                </button>
              </>
            )}
          </>
        )}
      </div>
    </div>
  )
}
