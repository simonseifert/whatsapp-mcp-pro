import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch } from './api'
import type { ChatsResponse } from './api-types'
import type { Filter } from './components/FilterChips'
import { subscribeLive } from './live'

type State = { data: ChatsResponse | null; error: boolean; loadedQuery: string | null; key: string }

/** Live events are coalesced into one refetch per this many ms (a burst of messages is one list refresh). */
const LIVE_REFRESH_MS = 300

/**
 * Loads /api/chats for the given filter, archive flag and (already debounced) search text; refreshes on live events.
 * `data.chats` is the first page plus any older pages `loadMore` fetched; `data.has_more` says whether older remain.
 */
export function useChats(filter: Filter, archived: boolean, query: string) {
  const key = `${filter}|${archived ? 1 : 0}|${query}`
  const [state, setState] = useState<State>({ data: null, error: false, loadedQuery: null, key })
  const [attempt, setAttempt] = useState(0)
  const latest = useRef(state)
  useEffect(() => {
    latest.current = state
  })
  const busy = useRef(false)
  const baseParams = useCallback(() => {
    const params = new URLSearchParams({ filter })
    if (archived) params.set('archived', '1')
    if (query) params.set('q', query)
    return params
  }, [filter, archived, query])

  useEffect(() => {
    let current = true
    apiFetch<ChatsResponse>(`/api/chats?${baseParams()}`)
      .then((first) => {
        if (!current) return
        setState((prev) => {
          // A refresh of the same list keeps the older pages already loaded (minus chats that moved up into the
          // first page); a different filter, archive or search starts over.
          const tail =
            prev.key === key && prev.data
              ? prev.data.chats.filter(
                  (c) => !c.pinned && !first.chats.some((f) => f.jid === c.jid) && isOlderThan(c, first.chats.at(-1)),
                )
              : []
          const chats = [...first.chats, ...tail]
          return {
            data: { ...first, chats, has_more: tail.length > 0 ? (prev.data?.has_more ?? false) : first.has_more },
            error: false,
            loadedQuery: query,
            key,
          }
        })
      })
      .catch(() => current && setState((prev) => ({ ...prev, error: true })))
    return () => {
      current = false
    }
  }, [baseParams, key, query, attempt])

  const loadMore = useCallback(() => {
    const { data, key: loadedKey } = latest.current
    const last = data?.chats.at(-1)
    if (busy.current || !data?.has_more || !last || loadedKey !== key) return
    busy.current = true
    const params = baseParams()
    params.set('before', last.last_message.timestamp_iso)
    apiFetch<ChatsResponse>(`/api/chats?${params}`)
      .then((page) => {
        setState((prev) => {
          if (prev.key !== key || !prev.data) return prev // the list changed meanwhile
          const known = new Set(prev.data.chats.map((c) => c.jid))
          const chats = [...prev.data.chats, ...page.chats.filter((c) => !known.has(c.jid))]
          return { ...prev, data: { ...prev.data, chats, has_more: page.has_more } }
        })
      })
      .catch(() => {
        // The sentinel asks again the next time it scrolls into view.
      })
      .finally(() => {
        busy.current = false
      })
  }, [key, baseParams])

  // The server decides what the filter, the search and the archive show, so a change refetches rather than patching:
  // that reorders the list, moves a chat between filters and keeps the archived count right.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null
    const unsubscribe = subscribeLive((event) => {
      if (event.name !== 'chat' && event.name !== 'resync') return
      timer ??= setTimeout(() => {
        timer = null
        setAttempt((n) => n + 1)
      }, LIVE_REFRESH_MS)
    })
    return () => {
      unsubscribe()
      if (timer) clearTimeout(timer)
    }
  }, [])

  const retry = useCallback(() => setAttempt((n) => n + 1), [])
  return { ...state, loadMore, retry }
}

function isOlderThan(chat: { last_message: { timestamp_iso: string } }, ref?: { last_message: { timestamp_iso: string } }) {
  return !ref || Date.parse(chat.last_message.timestamp_iso) < Date.parse(ref.last_message.timestamp_iso)
}
