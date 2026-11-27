import { useEffect, useMemo, useRef, useState } from 'react'
import { apiFetch, forwardMessages } from '../api'
import type { ChatsResponse, ChatSummary, ForwardResult } from '../api-types'
import { Avatar } from './Avatar'
import { Icon } from './Icon'
import { useDialog } from './useDialog'

export const MAX_FORWARD_CHATS = 5

/**
 * "Forward to": pick up to five chats and send. The answer is per chat: chats that failed stay listed with their
 * reason and Try again sends only to those, so a partial forward is never silent.
 */
export function ForwardSheet({
  fromChat,
  messageIds,
  onClose,
}: {
  fromChat: string
  messageIds: string[]
  onClose: () => void
}) {
  const [chats, setChats] = useState<ChatSummary[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [text, setText] = useState('')
  const [picked, setPicked] = useState<string[]>([])
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [results, setResults] = useState<ForwardResult[] | null>(null)
  const key = useRef(crypto.randomUUID()) // one key for the whole forward: a retry never sends a chat twice
  const dialog = useRef<HTMLDivElement>(null)
  useDialog(dialog, onClose)

  useEffect(() => {
    let live = true
    apiFetch<ChatsResponse>('/api/chats?filter=all')
      .then((res) => live && setChats(res.chats))
      .catch(() => live && setFailed(true))
    return () => {
      live = false
    }
  }, [])

  const names = useMemo(() => new Map((chats ?? []).map((c) => [c.jid, c.name])), [chats])
  const shown = useMemo(() => {
    const needle = text.trim().toLowerCase()
    return (chats ?? []).filter((c) => !needle || c.name.toLowerCase().includes(needle))
  }, [chats, text])
  const full = picked.length >= MAX_FORWARD_CHATS
  const failedChats = results?.filter((r) => !r.ok) ?? []
  const done = results !== null && failedChats.length === 0

  function toggle(jid: string) {
    setPicked((p) => (p.includes(jid) ? p.filter((j) => j !== jid) : p.length < MAX_FORWARD_CHATS ? [...p, jid] : p))
  }

  async function send(targets: string[]) {
    setSending(true)
    setError(null)
    const outcome = await forwardMessages(fromChat, messageIds, targets, key.current)
    setSending(false)
    if (!outcome.ok) {
      setError(outcome.error)
      return
    }
    // Keep what already went out; replace the entries of the chats that were just tried.
    setResults((prev) => [...(prev ?? []).filter((r) => !targets.includes(r.to)), ...outcome.results])
    setPicked(outcome.results.filter((r) => !r.ok).map((r) => r.to))
  }

  const label = (jid: string) => names.get(jid) ?? jid
  const count = (n: number) => `${n} ${n === 1 ? 'chat' : 'chats'}`

  return (
    <div className="menu-wrap sheet-wrap">
      <button className="menu-backdrop" type="button" aria-label="Close" onClick={onClose} tabIndex={-1} />
      <div className="sheet" role="dialog" aria-modal="true" aria-label="Forward to" ref={dialog}>
        <header className="sheet-head">
          <h2>Forward to</h2>
          <button className="iconbtn" type="button" aria-label="Close" onClick={onClose}>
            <Icon name="close" size={20} />
          </button>
        </header>
        {results ? (
          <>
            <ul className="fwd-results">
              {results.map((r) => (
                <li key={r.to} className={r.ok ? undefined : 'form-error'}>
                  <span dir="auto">{`${label(r.to)}: ${r.ok ? 'Forwarded' : (r.error ?? "Couldn't forward")}`}</span>
                </li>
              ))}
            </ul>
            {done ? (
              <>
                <p role="status">Forwarded to {count(results.length)}</p>
                <button className="button-text" type="button" onClick={onClose}>
                  Done
                </button>
              </>
            ) : (
              <button className="button-text" type="button" disabled={sending} onClick={() => void send(picked)}>
                Try again
              </button>
            )}
          </>
        ) : (
          <>
            <label className="search">
              <Icon name="search" size={19} />
              <span className="sr">Search chats</span>
              <input
                type="search"
                placeholder="Search"
                aria-label="Search chats"
                value={text}
                onChange={(e) => setText(e.target.value)}
                autoComplete="off"
              />
            </label>
            {failed && <p role="alert">Couldn&apos;t load your chats.</p>}
            {!chats && !failed && <p>Loading…</p>}
            {full && <p className="hint">You can pick up to {MAX_FORWARD_CHATS} chats.</p>}
            <ul className="fwd-chats">
              {shown.map((c) => (
                <li key={c.jid}>
                  <label className="sheet-row">
                    <Avatar name={c.name} src={c.avatar_url} kind={c.kind} />
                    <span dir="auto">{c.name}</span>
                    <input
                      type="checkbox"
                      aria-label={c.name}
                      checked={picked.includes(c.jid)}
                      disabled={full && !picked.includes(c.jid)}
                      onChange={() => toggle(c.jid)}
                    />
                  </label>
                </li>
              ))}
            </ul>
            {error && (
              <p className="form-error" role="alert">
                {error}
              </p>
            )}
            <button
              className="button-text"
              type="button"
              disabled={picked.length === 0 || sending}
              onClick={() => void send(picked)}
            >
              {sending ? 'Forwarding…' : `Forward to ${count(picked.length)}`}
            </button>
          </>
        )}
      </div>
    </div>
  )
}
