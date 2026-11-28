import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router'
import { fetchStarred } from '../api'
import type { StarredItem } from '../api-types'
import { Icon } from '../components/Icon'
import { formatListTime } from '../format'

const preview = (item: StarredItem) => {
  const m = item.message
  return m.text?.trim() || (m.media ? `[${m.media.type}]` : m.poll ? m.poll.question : '')
}

/** Starred messages across all chats, newest star first. A row opens its chat on a window around the message. */
export function Starred() {
  const [items, setItems] = useState<StarredItem[]>([])
  const [next, setNext] = useState<string | null>(null)
  const [status, setStatus] = useState<'loading' | 'done' | 'error'>('loading')
  const [loadingMore, setLoadingMore] = useState(false)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let live = true
    fetchStarred()
      .then((res) => {
        if (!live) return
        setItems(res.items)
        setNext(res.next)
        setStatus('done')
      })
      .catch(() => live && setStatus('error'))
    return () => {
      live = false
    }
  }, [attempt])

  const more = useCallback(async () => {
    if (!next || loadingMore) return
    setLoadingMore(true)
    try {
      const res = await fetchStarred(next)
      setItems((prev) => [...prev, ...res.items])
      setNext(res.next)
    } catch {
      setStatus('error')
    } finally {
      setLoadingMore(false)
    }
  }, [next, loadingMore])

  return (
    <div className="pane-scroll">
      <header className="lhead">
        <Link className="back" to="/search">
          <Icon name="back" size={22} />
          <span>Search</span>
        </Link>
        <h1 className="large-title">Starred messages</h1>
      </header>
      {status === 'loading' && <p className="empty-state">Loading…</p>}
      {status === 'error' && (
        <div className="empty-state" role="alert">
          <p>Could not load starred messages.</p>
          <button
            className="button-text"
            type="button"
            onClick={() => {
              setStatus('loading')
              setAttempt((n) => n + 1)
            }}
          >
            Try again
          </button>
        </div>
      )}
      {status === 'done' && items.length === 0 && <p className="empty-state">No starred messages yet.</p>}
      <ul className="ls">
        {items.map((item) => (
          <li key={`${item.message.chat_jid}/${item.message.id}`}>
            <Link
              className="row shit"
              to={`/chat/${encodeURIComponent(item.chat_jid)}?around=${encodeURIComponent(item.message.id)}`}
            >
              <div className="body">
                <div className="top">
                  <span className="name" dir="auto">
                    {item.chat_name}
                  </span>
                  <time className="time" dateTime={item.message.timestamp_iso}>
                    {formatListTime(item.message.timestamp_iso)}
                  </time>
                </div>
                <div className="prev">
                  <Icon name="star" size={14} />
                  <span dir="auto">
                    {item.message.from_me ? 'You: ' : item.message.sender_name ? `${item.message.sender_name}: ` : ''}
                    {preview(item)}
                  </span>
                </div>
              </div>
            </Link>
          </li>
        ))}
      </ul>
      {next && status === 'done' && (
        <button className="button-text more-results" type="button" disabled={loadingMore} onClick={() => void more()}>
          {loadingMore ? 'Loading…' : 'Show more'}
        </button>
      )}
    </div>
  )
}
