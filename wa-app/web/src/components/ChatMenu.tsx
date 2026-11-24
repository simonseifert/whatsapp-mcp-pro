import { useRef } from 'react'
import { Icon } from './Icon'
import { useDialog } from './useDialog'

/** The chat header's menu: one item for now, the AI settings. */
export function ChatMenu({ onAi, onClose }: { onAi: () => void; onClose: () => void }) {
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
      </div>
    </div>
  )
}
