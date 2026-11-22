import { useCallback, useEffect, useState } from 'react'
import { apiFetch } from './api'
import type { ChatsResponse } from './api-types'
import type { Filter } from './components/FilterChips'
import { subscribeLive } from './live'

type State = { data: ChatsResponse | null; error: boolean; loadedQuery: string | null }

/** Live events are coalesced into one refetch per this many ms (a burst of messages is one list refresh). */
const LIVE_REFRESH_MS = 300

/** Loads /api/chats for the given filter, archive flag and (already debounced) search text; refreshes on live events. */
export function useChats(filter: Filter, archived: boolean, query: string) {
  const [state, setState] = useState<State>({ data: null, error: false, loadedQuery: null })
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let current = true
    const params = new URLSearchParams({ filter })
    if (archived) params.set('archived', '1')
    if (query) params.set('q', query)
    apiFetch<ChatsResponse>(`/api/chats?${params}`)
      .then((data) => current && setState({ data, error: false, loadedQuery: query }))
      .catch(() => current && setState((prev) => ({ ...prev, error: true })))
    return () => {
      current = false
    }
  }, [filter, archived, query, attempt])

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
  return { ...state, retry }
}
