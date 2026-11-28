import { useCallback, useEffect, useState } from 'react'
import { createList, deleteList, fetchLists, updateList } from '../api'
import type { ChatList } from '../api-types'
import { Icon } from '../components/Icon'

const MAX_NAME = 40
const MAX_LISTS = 20

/** Settings > Lists: create, rename, reorder and delete custom chat lists. The order here is the order of the chips. */
export function Lists({ onBack }: { onBack: () => void }) {
  const [lists, setLists] = useState<ChatList[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [editing, setEditing] = useState<{ id: number; text: string } | null>(null)
  const [deleting, setDeleting] = useState<number | null>(null)

  const load = useCallback(() => {
    fetchLists()
      .then((rows) => {
        setLists(rows)
        setFailed(false)
      })
      .catch(() => setFailed(true))
  }, [])
  useEffect(load, [load])

  async function run(work: () => Promise<unknown>) {
    setError(null)
    try {
      await work()
      load()
      return true
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't save")
      return false
    }
  }

  async function add() {
    if (await run(() => createList(name.trim()))) setName('')
  }

  async function saveName() {
    if (!editing) return
    if (await run(() => updateList(editing.id, { name: editing.text.trim() }))) setEditing(null)
  }

  const full = (lists?.length ?? 0) >= MAX_LISTS

  return (
    <div className="pane-scroll">
      <header className="lhead">
        <button className="back" type="button" onClick={onBack}>
          <Icon name="back" size={22} />
          <span>Settings</span>
        </button>
        <h1 className="large-title">Lists</h1>
      </header>
      <div className="settings">
        <form
          className="settings-group"
          onSubmit={(e) => {
            e.preventDefault()
            if (name.trim() && !full) void add()
          }}
        >
          <label className="settings-label" htmlFor="new-list">
            New list
          </label>
          <input
            id="new-list"
            className="field"
            aria-label="New list name"
            placeholder="Name"
            maxLength={MAX_NAME}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <button className="button-text" type="button" disabled={!name.trim() || full} onClick={() => void add()}>
            Add list
          </button>
          {full && <p className="settings-about">You can have up to {MAX_LISTS} lists.</p>}
        </form>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        {failed && !lists && (
          <p role="alert">
            Couldn&apos;t load your lists.{' '}
            <button className="button-text" type="button" onClick={load}>
              Try again
            </button>
          </p>
        )}
        {lists?.length === 0 && <p className="settings-about">No lists yet. A list groups chats under a chip on the chat list.</p>}
        <ul className="ls">
          {lists?.map((list, index) => (
            <li key={list.id} className="list-row">
              {editing?.id === list.id ? (
                <form
                  className="list-edit"
                  onSubmit={(e) => {
                    e.preventDefault()
                    void saveName()
                  }}
                >
                  <input
                    className="field"
                    aria-label="List name"
                    maxLength={MAX_NAME}
                    value={editing.text}
                    onChange={(e) => setEditing({ id: list.id, text: e.target.value })}
                  />
                  <button className="button-text" type="button" disabled={!editing.text.trim()} onClick={() => void saveName()}>
                    Save
                  </button>
                  <button className="button-text neutral" type="button" onClick={() => setEditing(null)}>
                    Cancel
                  </button>
                </form>
              ) : deleting === list.id ? (
                <div className="list-edit">
                  <span>Delete {list.name}? The chats stay.</span>
                  <button
                    className="button-text danger"
                    type="button"
                    onClick={() => void run(() => deleteList(list.id)).then(() => setDeleting(null))}
                  >
                    Yes, delete
                  </button>
                  <button className="button-text neutral" type="button" onClick={() => setDeleting(null)}>
                    Cancel
                  </button>
                </div>
              ) : (
                <div className="list-edit">
                  <span dir="auto" className="list-name">
                    {list.name}
                    <span className="settings-about"> {list.count === 1 ? '1 chat' : `${list.count} chats`}</span>
                  </span>
                  <button
                    className="iconbtn"
                    type="button"
                    aria-label={`Move ${list.name} up`}
                    disabled={index === 0}
                    onClick={() => void run(() => updateList(list.id, { position: index - 1 }))}
                  >
                    <Icon name="up" size={20} />
                  </button>
                  <button
                    className="iconbtn"
                    type="button"
                    aria-label={`Move ${list.name} down`}
                    disabled={index === lists.length - 1}
                    onClick={() => void run(() => updateList(list.id, { position: index + 1 }))}
                  >
                    <Icon name="down" size={20} />
                  </button>
                  <button
                    className="button-text neutral"
                    type="button"
                    aria-label={`Rename ${list.name}`}
                    onClick={() => setEditing({ id: list.id, text: list.name })}
                  >
                    Rename
                  </button>
                  <button
                    className="button-text danger"
                    type="button"
                    aria-label={`Delete ${list.name}`}
                    onClick={() => setDeleting(list.id)}
                  >
                    Delete
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
