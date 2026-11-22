import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, apiFetch } from './api'
import type { ChatDetail, LiveEvent, MessageView, MessagesResponse } from './api-types'
import { subscribeLive } from './live'
import { mergePage, placeMessage } from './liveMessages'

type State = {
  detail: ChatDetail | null
  messages: MessageView[]
  hasMore: boolean
  status: 'loading' | 'ready' | 'missing' | 'error'
  loadingOlder: boolean
}

/** Most live events held during the first load; beyond it the chat refetches once instead. */
const MAX_BUFFERED = 200

/** Called when a message joined the chat over the live stream. `newest`: it is now the last message. */
export type OnArrival = (message: MessageView, newest: boolean) => void

/** The chat header data plus the newest page of messages, and a way to page further back. Live events keep it current. */
export function useChat(jid: string, onArrival?: OnArrival) {
  const [state, setStateRaw] = useState<State>({
    detail: null,
    messages: [],
    hasMore: false,
    status: 'loading',
    loadingOlder: false,
  })
  // The latest state, readable from event handlers (a state updater must not have side effects).
  const latest = useRef(state)
  const setState = useCallback((update: State | ((s: State) => State)) => {
    latest.current = typeof update === 'function' ? update(latest.current) : update
    setStateRaw(latest.current)
  }, [])
  const [attempt, setAttempt] = useState(0)
  const busy = useRef(false)
  // Live events that arrive while the first load is still running: applied when it finishes, not dropped.
  const buffered = useRef<LiveEvent[]>([])
  const handler = useRef<(event: LiveEvent) => void>(() => {})
  const arrival = useRef(onArrival)
  useEffect(() => {
    arrival.current = onArrival
  })
  const path = `/api/chats/${encodeURIComponent(jid)}`

  useEffect(() => {
    let current = true
    buffered.current = []
    Promise.all([apiFetch<ChatDetail>(path), apiFetch<MessagesResponse>(`${path}/messages`)])
      .then(([detail, page]) => {
        if (!current) return
        setState({ detail, messages: page.messages, hasMore: page.has_more, status: 'ready', loadingOlder: false })
        const waiting = buffered.current
        buffered.current = []
        for (const event of waiting) handler.current(event) // a duplicate of a loaded message only replaces it
      })
      .catch((err: unknown) => {
        if (current) setState((s) => ({ ...s, status: err instanceof ApiError && err.status === 404 ? 'missing' : 'error' }))
      })
    return () => {
      current = false
    }
  }, [path, attempt, setState])

  // Live events. The event's chat_jid is the one the chat list uses; the route may name another form of it.
  useEffect(() => {
    let alive = true
    const mine = (chatJid: string) => chatJid === jid || chatJid === latest.current.detail?.chat.jid

    /** Fetches one message again (an edit, a delete or a reaction changed it) and swaps it in. */
    async function refreshMessage(id: string) {
      try {
        const { message } = await apiFetch<{ message: MessageView }>(`${path}/messages/${encodeURIComponent(id)}`)
        if (!alive) return
        setState((s) => ({ ...s, messages: placeMessage(s.messages, message, s.hasMore).messages }))
      } catch {
        // A message that cannot be fetched keeps what it showed; the next event or reconnect refetches.
      }
    }

    /** After the stream was out of reach: refetch the newest page and merge it in, keeping what is loaded. */
    async function resync() {
      try {
        const [detail, page] = await Promise.all([
          apiFetch<ChatDetail>(path),
          apiFetch<MessagesResponse>(`${path}/messages`),
        ])
        if (!alive) return
        setState((s) => ({ ...s, detail, messages: mergePage(s.messages, page.messages), status: 'ready' }))
      } catch {
        // Offline still: the next visibility change or reconnect tries again.
      }
    }

    function onEvent(event: LiveEvent) {
      if (latest.current.status === 'loading') {
        if (buffered.current.length < MAX_BUFFERED) buffered.current.push(event)
        else buffered.current = [{ name: 'resync', data: {} }] // too many to replay: refetch once instead
        return
      }
      if (latest.current.status !== 'ready') return
      if (event.name === 'message' && mine(event.data.chat_jid)) {
        const placed = placeMessage(latest.current.messages, event.data.message, latest.current.hasMore)
        setState((s) => ({ ...s, messages: placed.messages }))
        if (placed.added) arrival.current?.(event.data.message, placed.newest)
      } else if (event.name === 'message_changed' && mine(event.data.chat_jid)) {
        void refreshMessage(event.data.id)
      } else if (event.name === 'reaction' && mine(event.data.chat_jid)) {
        void refreshMessage(event.data.message_id)
      } else if (event.name === 'chat' && mine(event.data.chat.jid)) {
        const chat = event.data.chat
        setState((s) => (s.detail ? { ...s, detail: { ...s.detail, chat } } : s))
      } else if (event.name === 'resync') {
        void resync()
      }
    }
    handler.current = onEvent
    const unsubscribe = subscribeLive(onEvent)
    return () => {
      alive = false
      unsubscribe()
    }
  }, [jid, path, setState])

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
  }, [path, oldest, state.hasMore, setState])

  const retry = useCallback(() => setAttempt((n) => n + 1), [])
  return { ...state, loadOlder, retry }
}
