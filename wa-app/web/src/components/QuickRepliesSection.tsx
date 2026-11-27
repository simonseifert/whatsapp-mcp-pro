import { useCallback, useEffect, useState } from 'react'
import { createQuickReply, deleteQuickReply, fetchQuickReplies, updateQuickReply } from '../api'
import type { QuickReply } from '../api-types'

const MAX_TEXT = 4096
const SHORTCUT = /^[a-z0-9_-]{1,24}$/

/** Settings > Quick replies: a shortcut that expands to a saved text when typed after "/" in a chat. */
export function QuickRepliesSection() {
  const [rows, setRows] = useState<QuickReply[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [shortcut, setShortcut] = useState('')
  const [text, setText] = useState('')
  const [editing, setEditing] = useState<{ shortcut: string; text: string } | null>(null)
  const [deleting, setDeleting] = useState<string | null>(null)

  const load = useCallback(() => {
    fetchQuickReplies()
      .then((r) => {
        setRows(r)
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

  const validNew = SHORTCUT.test(shortcut) && text.trim().length > 0 && text.length <= MAX_TEXT

  async function add() {
    if (await run(() => createQuickReply(shortcut, text.trim()))) {
      setShortcut('')
      setText('')
    }
  }

  return (
    <div className="settings-group">
      <div className="settings-label">Quick replies</div>
      <p className="settings-about">Type / in a chat, then a shortcut, to insert the text. Nothing is sent until you tap send.</p>
      <form
        className="qr-form"
        onSubmit={(e) => {
          e.preventDefault()
          if (validNew) void add()
        }}
      >
        <input
          className="field"
          aria-label="New shortcut"
          placeholder="Shortcut, like thanks"
          maxLength={24}
          autoCapitalize="none"
          value={shortcut}
          onChange={(e) => setShortcut(e.target.value)}
        />
        <textarea
          className="field"
          aria-label="New text"
          placeholder="Text"
          rows={2}
          maxLength={MAX_TEXT}
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <button className="button-text" type="button" disabled={!validNew} onClick={() => void add()}>
          Add quick reply
        </button>
        {shortcut && !SHORTCUT.test(shortcut) && (
          <p className="settings-about">A shortcut uses a to z in lower case, 0 to 9, _ or -, up to 24 characters.</p>
        )}
      </form>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      {failed && !rows && (
        <p role="alert">
          Couldn&apos;t load your quick replies.{' '}
          <button className="button-text" type="button" onClick={load}>
            Try again
          </button>
        </p>
      )}
      {rows?.length === 0 && <p className="settings-about">No quick replies yet.</p>}
      <ul className="ls">
        {rows?.map((r) => (
          <li key={r.shortcut} className="list-row">
            {editing?.shortcut === r.shortcut ? (
              <form
                className="list-edit"
                onSubmit={(e) => {
                  e.preventDefault()
                }}
              >
                <textarea
                  className="field"
                  aria-label="Text"
                  rows={3}
                  maxLength={MAX_TEXT}
                  value={editing.text}
                  onChange={(e) => setEditing({ shortcut: r.shortcut, text: e.target.value })}
                />
                <button
                  className="button-text"
                  type="button"
                  disabled={!editing.text.trim()}
                  onClick={() => void run(() => updateQuickReply(r.shortcut, editing.text.trim())).then((ok) => ok && setEditing(null))}
                >
                  Save
                </button>
                <button className="button-text neutral" type="button" onClick={() => setEditing(null)}>
                  Cancel
                </button>
              </form>
            ) : deleting === r.shortcut ? (
              <div className="list-edit">
                <span>Delete /{r.shortcut}?</span>
                <button
                  className="button-text danger"
                  type="button"
                  onClick={() => void run(() => deleteQuickReply(r.shortcut)).then(() => setDeleting(null))}
                >
                  Yes, delete
                </button>
                <button className="button-text neutral" type="button" onClick={() => setDeleting(null)}>
                  Cancel
                </button>
              </div>
            ) : (
              <div className="list-edit">
                <span className="list-name">
                  <b>/{r.shortcut}</b>
                  <span className="settings-about" dir="auto">
                    {r.text}
                  </span>
                </span>
                <button
                  className="button-text neutral"
                  type="button"
                  aria-label={`Edit /${r.shortcut}`}
                  onClick={() => setEditing({ shortcut: r.shortcut, text: r.text })}
                >
                  Edit
                </button>
                <button
                  className="button-text danger"
                  type="button"
                  aria-label={`Delete /${r.shortcut}`}
                  onClick={() => setDeleting(r.shortcut)}
                >
                  Delete
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
