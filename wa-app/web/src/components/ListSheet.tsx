import { useEffect, useRef, useState } from 'react'
import { changeListChats, fetchLists } from '../api'
import type { ChatList } from '../api-types'
import { Icon } from './Icon'
import { useDialog } from './useDialog'

/** "Add to list": one switch per custom list, saved as it changes. */
export function ListSheet({ jid, onClose }: { jid: string; onClose: () => void }) {
  const [rows, setRows] = useState<ChatList[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const dialog = useRef<HTMLDivElement>(null)
  useDialog(dialog, onClose)

  useEffect(() => {
    let live = true
    fetchLists(jid)
      .then((lists) => live && setRows(lists))
      .catch(() => live && setFailed(true))
    return () => {
      live = false
    }
  }, [jid])

  async function toggle(list: ChatList, on: boolean) {
    setError(null)
    setRows((prev) => prev && prev.map((r) => (r.id === list.id ? { ...r, member: on } : r)))
    try {
      await changeListChats(list.id, on ? { add: [jid], remove: [] } : { add: [], remove: [jid] })
    } catch {
      setRows((prev) => prev && prev.map((r) => (r.id === list.id ? { ...r, member: !on } : r)))
      setError(`Couldn't update ${list.name}`)
    }
  }

  return (
    <div className="menu-wrap sheet-wrap">
      <button className="menu-backdrop" type="button" aria-label="Close" onClick={onClose} tabIndex={-1} />
      <div className="sheet" role="dialog" aria-modal="true" aria-label="Add to list" ref={dialog}>
        <header className="sheet-head">
          <h2>Add to list</h2>
          <button className="iconbtn" type="button" aria-label="Close" onClick={onClose}>
            <Icon name="close" size={20} />
          </button>
        </header>
        {failed && <p role="alert">Couldn&apos;t load your lists.</p>}
        {!rows && !failed && <p>Loading…</p>}
        {rows?.length === 0 && <p>No lists yet. Create one in Settings.</p>}
        {rows?.map((list) => (
          <label className="sheet-row" key={list.id}>
            <span dir="auto">{list.name}</span>
            <input
              type="checkbox"
              role="switch"
              aria-label={list.name}
              checked={list.member === true}
              onChange={(e) => void toggle(list, e.target.checked)}
            />
          </label>
        ))}
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </div>
    </div>
  )
}
