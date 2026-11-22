import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, apiFetch } from './api'
import type { ChatDetail, MessageView, MessagesResponse } from './api-types'

type State = {
  detail: ChatDetail | null
  messages: MessageView[]
  hasMore: boolean
  status: 'loading' | 'ready' | 'missing' | 'error'
  loadingOlder: boolean
}

/** The chat header data plus the newest page of messages, and a way to page further back. */
export function useChat(jid: string) {
  const [state, setState] = useState<State>({
    detail: null,
    messages: [],
    hasMore: false,
    status: 'loading',
    loadingOlder: false,
  })
  const [attempt, setAttempt] = useState(0)
  const busy = useRef(false)
  const path = `/api/chats/${encodeURIComponent(jid)}`

  useEffect(() => {
    let current = true
    Promise.all([apiFetch<ChatDetail>(path), apiFetch<MessagesResponse>(`${path}/messages`)])
      .then(([detail, page]) => {
        if (current)
          setState({ detail, messages: page.messages, hasMore: page.has_more, status: 'ready', loadingOlder: false })
      })
      .catch((err: unknown) => {
        if (current) setState((s) => ({ ...s, status: err instanceof ApiError && err.status === 404 ? 'missing' : 'error' }))
      })
    return () => {
      current = false
    }
  }, [path, attempt])

  const oldest = state.messages[0]?.timestamp_iso
  /** Loads the page before the oldest message; true only when a page arrived (false: not started, or it failed). */
  const loadOlder = useCallback(async (): Promise<boolean> => {
    if (busy.current || !state.hasMore || !oldest) return false
    busy.current = true
    setState((s) => ({ ...s, loadingOlder: true }))
    try {
      const page = await apiFetch<MessagesResponse>(`${path}/messages?before=${encodeURIComponent(oldest)}`)
      setState((s) => {
        const known = new Set(s.messages.map((m) => `${m.chat_jid}/${m.id}`))
        const older = page.messages.filter((m) => !known.has(`${m.chat_jid}/${m.id}`))
        return { ...s, messages: [...older, ...s.messages], hasMore: page.has_more, loadingOlder: false }
      })
      return true
    } catch {
      setState((s) => ({ ...s, loadingOlder: false }))
      return false
    } finally {
      busy.current = false
    }
  }, [path, oldest, state.hasMore])

  const retry = useCallback(() => setAttempt((n) => n + 1), [])
  return { ...state, loadOlder, retry }
}
