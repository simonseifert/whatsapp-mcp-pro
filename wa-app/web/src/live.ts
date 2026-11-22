import type { LiveEvent } from './api-types'

export const BACKOFF_START_MS = 1000
export const BACKOFF_MAX_MS = 30000
/** While visible, ask listeners to refetch this often: covers what the server's poller cannot see. */
export const SAFETY_RESYNC_MS = 5 * 60 * 1000

const EVENT_NAMES = ['message', 'message_changed', 'reaction', 'chat', 'resync'] as const

/** Delay before reconnect attempt number `attempt` (0 = first): 1 s, doubling, capped at 30 s. */
export function backoffDelay(attempt: number): number {
  return Math.min(BACKOFF_START_MS * 2 ** attempt, BACKOFF_MAX_MS)
}

type Source = {
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void
  close(): void
  onerror: ((event: Event) => void) | null
}
export type SourceFactory = (url: string) => Source

const defaultFactory: SourceFactory = (url) => new EventSource(url, { withCredentials: true }) as unknown as Source

/**
 * Opens the live stream and keeps it open: reconnects with backoff, resumes from the last event id, and when the
 * page comes back to the foreground reconnects at once and asks listeners to refetch (a suspended PWA's stream dies
 * silently and the server cannot tell what was missed). Reports page visibility to the server. Returns a stop function.
 */
export function connectLive(
  onEvent: (event: LiveEvent) => void,
  options: { factory?: SourceFactory; report?: (client: string, visible: boolean) => void } = {},
): () => void {
  const factory = options.factory ?? defaultFactory
  const report = options.report ?? reportVisibility
  let source: Source | null = null
  let timer: ReturnType<typeof setTimeout> | null = null
  let attempt = 0
  let lastId = ''
  let client = ''
  let stopped = false

  function open() {
    source?.close()
    if (timer) clearTimeout(timer)
    timer = null
    const next = factory(lastId ? `/api/live?last_event_id=${encodeURIComponent(lastId)}` : '/api/live')
    source = next
    next.addEventListener('hello', (e) => {
      if (e.lastEventId) lastId = e.lastEventId // the server's current cursor: a drop before any event still resumes
      attempt = 0
      client = (JSON.parse(e.data) as { client: string }).client
      report(client, document.visibilityState === 'visible')
    })
    for (const name of EVENT_NAMES) {
      next.addEventListener(name, (e) => {
        if (e.lastEventId) lastId = e.lastEventId
        onEvent({ name, data: JSON.parse(e.data) } as LiveEvent)
      })
    }
    next.onerror = () => {
      if (stopped || source !== next) return
      next.close()
      source = null
      timer = setTimeout(() => {
        const resumable = lastId !== ''
        open()
        if (!resumable) onEvent({ name: 'resync', data: {} }) // nothing to resume from: refetch instead
      }, backoffDelay(attempt))
      attempt += 1
    }
  }

  let safety: ReturnType<typeof setInterval> | null = null
  function startSafety(visible: boolean) {
    if (safety) clearInterval(safety)
    safety = visible ? setInterval(() => onEvent({ name: 'resync', data: {} }), SAFETY_RESYNC_MS) : null
  }

  function onVisibility() {
    const visible = document.visibilityState === 'visible'
    if (client) report(client, visible)
    startSafety(visible)
    if (!visible) return
    attempt = 0
    open() // the old stream may be dead without having said so
    onEvent({ name: 'resync', data: {} })
  }

  document.addEventListener('visibilitychange', onVisibility)
  startSafety(document.visibilityState === 'visible')
  open()
  return () => {
    stopped = true
    document.removeEventListener('visibilitychange', onVisibility)
    if (timer) clearTimeout(timer)
    if (safety) clearInterval(safety)
    source?.close()
  }
}

function reportVisibility(client: string, visible: boolean) {
  void fetch('/api/live/visibility', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ client, visible }),
    keepalive: true,
  }).catch(() => {})
}

// One shared connection for every screen: opened with the first subscriber, closed with the last.
const listeners = new Set<(event: LiveEvent) => void>()
let stop: (() => void) | null = null

export function subscribeLive(listener: (event: LiveEvent) => void): () => void {
  listeners.add(listener)
  stop ??= connectLive((event) => listeners.forEach((l) => l(event)))
  return () => {
    listeners.delete(listener)
    if (listeners.size === 0) {
      stop?.()
      stop = null
    }
  }
}
