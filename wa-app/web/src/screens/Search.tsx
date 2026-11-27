import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { searchMessages } from '../api'
import type { SearchHit } from '../api-types'
import { Icon } from '../components/Icon'
import { Snippet } from '../components/Snippet'
import { formatListTime } from '../format'
import { MIN_QUERY } from '../snippet'

const DEBOUNCE_MS = 250

type Group = { chat_jid: string; chat_name: string; hits: SearchHit[] }

/** Results grouped by chat, in the order each chat first appears (newest result first). */
export function groupByChat(hits: SearchHit[]): Group[] {
  const groups = new Map<string, Group>()
  for (const hit of hits) {
    const group = groups.get(hit.chat_jid) ?? { chat_jid: hit.chat_jid, chat_name: hit.chat_name, hits: [] }
    group.hits.push(hit)
    groups.set(hit.chat_jid, group)
  }
  return [...groups.values()]
}

export function Search() {
  const [params, setParams] = useSearchParams()
  const urlQuery = params.get('q') ?? ''
  const [text, setText] = useState(urlQuery)
  const [query, setQuery] = useState(urlQuery.trim())
  const [hits, setHits] = useState<SearchHit[]>([])
  const [next, setNext] = useState<string | null>(null)
  const [status, setStatus] = useState<'idle' | 'loading' | 'done' | 'error'>('idle')
  const [attempt, setAttempt] = useState(0)
  const [loadingMore, setLoadingMore] = useState(false)
  const latest = useRef(0) // a slower answer to an older query must never replace a newer one
  const input = useRef<HTMLInputElement>(null)

  useEffect(() => input.current?.focus(), [])

  useEffect(() => {
    const id = window.setTimeout(() => {
      const trimmed = text.trim()
      setQuery(trimmed)
      setParams(trimmed ? { q: trimmed } : {}, { replace: true })
    }, DEBOUNCE_MS)
    return () => window.clearTimeout(id)
  }, [text, setParams])

  useEffect(() => {
    const ticket = ++latest.current
    if (query.length < MIN_QUERY) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- too short to search: clear the previous answer
      setHits([])
      setNext(null)
      setStatus('idle')
      return
    }
    setStatus('loading')
    searchMessages(query)
      .then((res) => {
        if (ticket !== latest.current) return
        setHits(res.hits)
        setNext(res.next)
        setStatus('done')
      })
      .catch(() => {
        if (ticket === latest.current) setStatus('error')
      })
  }, [query, attempt])

  const loadMore = useCallback(async () => {
    if (!next || loadingMore) return
    const ticket = latest.current
    setLoadingMore(true)
    try {
      const res = await searchMessages(query, { before: next })
      if (ticket !== latest.current) return
      setHits((h) => [...h, ...res.hits])
      setNext(res.next)
    } catch {
      if (ticket === latest.current) setStatus('error')
    } finally {
      setLoadingMore(false)
    }
  }, [next, loadingMore, query])

  const groups = useMemo(() => groupByChat(hits), [hits])

  return (
    <div className="pane-scroll">
      <header className="lhead">
        <Link className="back" to="/">
          <Icon name="back" size={22} />
          <span>Chats</span>
        </Link>
        <h1 className="large-title">Search</h1>
        <label className="search">
          <Icon name="search" size={19} />
          <span className="sr">Search messages</span>
          <input
            ref={input}
            type="search"
            placeholder="Search messages"
            value={text}
            onChange={(e) => setText(e.target.value)}
            autoComplete="off"
            enterKeyHint="search"
          />
        </label>
      </header>
      {status === 'idle' && <p className="empty-state">Type at least {MIN_QUERY} letters to search your messages.</p>}
      {status === 'loading' && <p className="empty-state">Searching…</p>}
      {status === 'error' && (
        <div className="empty-state" role="alert">
          <p>Could not search.</p>
          <button className="button-text" type="button" onClick={() => setAttempt((n) => n + 1)}>
            Try again
          </button>
        </div>
      )}
      {status === 'done' && hits.length === 0 && <p className="empty-state">No messages found.</p>}
      {groups.map((group) => (
        <section className="sgroup" key={group.chat_jid} aria-label={group.chat_name}>
          <h2 className="shead" dir="auto">
            {group.chat_name}
          </h2>
          <ul className="ls">
            {group.hits.map((hit) => (
              <li key={`${hit.chat_jid}/${hit.message_id}`}>
                <Link
                  className="row shit"
                  to={`/chat/${encodeURIComponent(hit.chat_jid)}?around=${encodeURIComponent(hit.message_id)}`}
                >
                  <div className="body">
                    <div className="top">
                      <span className="name" dir="auto">
                        {hit.sender_name ?? ''}
                      </span>
                      <time className="time" dateTime={hit.ts}>
                        {formatListTime(hit.ts)}
                      </time>
                    </div>
                    <div className="prev">
                      <Snippet text={hit.snippet} />
                    </div>
                  </div>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      ))}
      {next && (
        <button className="button-text more-results" type="button" disabled={loadingMore} onClick={() => void loadMore()}>
          {loadingMore ? 'Loading…' : 'Show more results'}
        </button>
      )}
    </div>
  )
}
