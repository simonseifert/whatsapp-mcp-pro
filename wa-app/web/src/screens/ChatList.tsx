import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router'
import { ChatRow } from '../components/ChatRow'
import { FilterChips, type Filter } from '../components/FilterChips'
import { Icon } from '../components/Icon'
import { fold } from '../format'
import { useChats } from '../useChats'

const SEARCH_DEBOUNCE_MS = 250

export function ChatList({ archived = false }: { archived?: boolean }) {
  const [filter, setFilter] = useState<Filter>('all')
  const [text, setText] = useState('')
  const [query, setQuery] = useState('')
  const { data, error, loadedQuery, retry } = useChats(filter, archived, query)

  useEffect(() => {
    const id = window.setTimeout(() => setQuery(text.trim()), SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(id)
  }, [text])

  // Instant client-side narrowing, only while the debounced server query for this text is still on its way.
  // Once the server has answered, its result stands (it also matches by number, which names do not show);
  // all-digit text is never filtered here for the same reason.
  const chats = useMemo(() => {
    const trimmed = text.trim()
    const all = data?.chats ?? []
    if (!trimmed || loadedQuery === trimmed || /^[+\d\s()-]+$/.test(trimmed)) return all
    const needle = fold(trimmed)
    return all.filter((c) => fold(c.name).includes(needle))
  }, [data, text, loadedQuery])

  const showArchivedRow = !archived && filter === 'all' && text.trim() === '' && (data?.archived_count ?? 0) > 0

  return (
    <div className="pane-scroll">
      <header className="lhead">
        {archived && (
          <Link className="back" to="/">
            <Icon name="back" size={22} />
            <span>Chats</span>
          </Link>
        )}
        <h1 className="large-title">{archived ? 'Archived' : 'Chats'}</h1>
        <label className="search">
          <Icon name="search" size={19} />
          <span className="sr">Search chats</span>
          <input
            type="search"
            placeholder="Search"
            value={text}
            onChange={(e) => setText(e.target.value)}
            autoComplete="off"
            enterKeyHint="search"
          />
        </label>
      </header>
      {!archived && <FilterChips value={filter} onChange={setFilter} />}
      {error && !data && (
        <div className="empty-state" role="alert">
          <p>Could not load chats.</p>
          <button className="button-text" type="button" onClick={retry}>
            Try again
          </button>
        </div>
      )}
      {data && (
        <ul className="ls">
          {showArchivedRow && (
            <li>
              <Link className="row arch" to="/archived">
                <div className="av" aria-hidden="true">
                  <Icon name="archive" size={24} />
                </div>
                <div className="body">
                  <span>Archived</span>
                  <span className="cnt">
                    {data.archived_count}
                    <Icon name="chevron" size={16} />
                  </span>
                </div>
              </Link>
            </li>
          )}
          {chats.map((chat) => (
            <ChatRow key={chat.jid} chat={chat} />
          ))}
        </ul>
      )}
      {data && chats.length === 0 && (
        <p className="empty-state">{text.trim() ? 'No chats match your search.' : archived ? 'No archived chats.' : 'No chats here.'}</p>
      )}
    </div>
  )
}
