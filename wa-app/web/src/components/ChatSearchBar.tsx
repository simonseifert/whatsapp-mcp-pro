import { useEffect, useRef, useState } from 'react'
import { searchMessages } from '../api'
import type { SearchHit } from '../api-types'
import { MIN_QUERY } from '../snippet'
import { Icon } from './Icon'

const DEBOUNCE_MS = 250

/**
 * Search inside one chat. Hits come newest first; "Up" goes to the next older one, "Down" to the next newer one.
 * `onJump` is told which message to bring into view each time the current hit changes.
 */
export function ChatSearchBar({
  chat,
  onJump,
  onClose,
}: {
  chat: string
  onJump: (messageId: string) => void
  onClose: () => void
}) {
  const [text, setText] = useState('')
  const [hits, setHits] = useState<SearchHit[]>([])
  const [next, setNext] = useState<string | null>(null)
  const [index, setIndex] = useState(0)
  const [state, setState] = useState<'idle' | 'loading' | 'done' | 'error'>('idle')
  const latest = useRef(0)
  const jump = useRef(onJump)
  useEffect(() => {
    jump.current = onJump
  })

  useEffect(() => {
    const query = text.trim()
    const ticket = ++latest.current
    if (query.length < MIN_QUERY) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- too short to search: clear the previous answer
      setHits([])
      setNext(null)
      setIndex(0)
      setState('idle')
      return
    }
    const id = window.setTimeout(() => {
      setState('loading')
      searchMessages(query, { chat })
        .then((res) => {
          if (ticket !== latest.current) return
          setHits(res.hits)
          setNext(res.next)
          setIndex(0)
          setState('done')
          if (res.hits[0]) jump.current(res.hits[0].message_id)
        })
        .catch(() => {
          if (ticket === latest.current) setState('error')
        })
    }, DEBOUNCE_MS)
    return () => window.clearTimeout(id)
  }, [text, chat])

  async function older() {
    if (index + 1 < hits.length) {
      setIndex(index + 1)
      jump.current(hits[index + 1].message_id)
      return
    }
    if (!next) return
    const ticket = latest.current
    try {
      const res = await searchMessages(text.trim(), { chat, before: next })
      if (ticket !== latest.current || res.hits.length === 0) return
      setHits((h) => [...h, ...res.hits])
      setNext(res.next)
      setIndex(index + 1)
      jump.current(res.hits[0].message_id)
    } catch {
      setState('error')
    }
  }
  function newer() {
    if (index === 0) return
    setIndex(index - 1)
    jump.current(hits[index - 1].message_id)
  }

  const label =
    state === 'loading'
      ? 'Searching…'
      : state === 'error'
        ? 'Could not search'
        : state === 'done'
          ? hits.length === 0
            ? 'No results'
            : `${index + 1} of ${hits.length}${next ? '+' : ''}`
          : ''
  return (
    <div className="csearch" role="search">
      <label className="csearch-field">
        <span className="sr">Search in this chat</span>
        <input
          type="search"
          placeholder="Search in this chat"
          value={text}
          autoFocus
          autoComplete="off"
          enterKeyHint="search"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              if (e.shiftKey) newer()
              else void older()
            } else if (e.key === 'Escape') onClose()
          }}
        />
      </label>
      <span className="csearch-count" aria-live="polite">
        {label}
      </span>
      <button
        className="iconbtn"
        type="button"
        aria-label="Older result"
        disabled={index + 1 >= hits.length && !next}
        onClick={() => void older()}
      >
        <Icon name="up" size={22} />
      </button>
      <button className="iconbtn" type="button" aria-label="Newer result" disabled={index === 0} onClick={newer}>
        <Icon name="down" size={22} />
      </button>
      <button className="iconbtn" type="button" aria-label="Close search" onClick={onClose}>
        <Icon name="close" size={22} />
      </button>
    </div>
  )
}
