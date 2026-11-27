import { useRef } from 'react'
import { Icon } from './Icon'
import { useDialog } from './useDialog'

/** The chat header's menu: search in this chat, add it to a list, and the AI settings. */
export function ChatMenu({
  onAi,
  onSearch,
  onLists,
  onClose,
}: {
  onAi: () => void
  onSearch?: () => void
  onLists?: () => void
  onClose: () => void
}) {
  const dialog = useRef<HTMLDivElement>(null)
  useDialog(dialog, onClose)
  return (
    <div className="menu-wrap menu-wrap-top-right">
      <button className="menu-backdrop" type="button" aria-label="Close menu" onClick={onClose} tabIndex={-1} />
      <div className="menu" role="dialog" aria-modal="true" aria-label="Chat actions" ref={dialog}>
        <button className="menu-item" type="button" onClick={onAi}>
          <Icon name="sparkle" size={20} />
          AI drafts
        </button>
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
      </div>
    </div>
  )
}
