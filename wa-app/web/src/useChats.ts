import { useCallback, useEffect, useState } from 'react'
import { apiFetch } from './api'
import type { ChatsResponse } from './api-types'
import type { Filter } from './components/FilterChips'

type State = { data: ChatsResponse | null; error: boolean }

/** Loads /api/chats for the given filter, archive flag and (already debounced) search text. */
export function useChats(filter: Filter, archived: boolean, query: string) {
  const [state, setState] = useState<State>({ data: null, error: false })
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let current = true
    const params = new URLSearchParams({ filter })
    if (archived) params.set('archived', '1')
    if (query) params.set('q', query)
    apiFetch<ChatsResponse>(`/api/chats?${params}`)
      .then((data) => current && setState({ data, error: false }))
      .catch(() => current && setState((prev) => ({ ...prev, error: true })))
    return () => {
      current = false
    }
  }, [filter, archived, query, attempt])

  const retry = useCallback(() => setAttempt((n) => n + 1), [])
  return { ...state, retry }
}
