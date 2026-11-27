// Service worker: caches the app shell only (index, JS, CSS, fonts, icons), versioned by the build.
// /api/* (chats, messages, media, the live stream) is never touched: the worker does not answer those at all.
// vite.config.ts replaces the two placeholders below at build time.

interface WorkerScope {
  skipWaiting(): Promise<void>
  clients: {
    claim(): Promise<void>
    matchAll(options: { type: 'window'; includeUncontrolled: boolean }): Promise<WindowClientLike[]>
    openWindow(url: string): Promise<unknown>
  }
  registration: {
    showNotification(title: string, options: { body?: string; tag?: string; renotify?: boolean; data?: { url: string } }): Promise<void>
  }
  addEventListener(type: 'install' | 'activate', listener: (event: WaitEvent) => void): void
  addEventListener(type: 'push', listener: (event: PushEv) => void): void
  addEventListener(type: 'notificationclick', listener: (event: ClickEv) => void): void
  addEventListener(type: 'fetch', listener: (event: FetchEv) => void): void
}
interface WaitEvent {
  waitUntil(work: Promise<unknown>): void
}
interface WindowClientLike {
  focus(): Promise<unknown>
  navigate(url: string): Promise<unknown>
}
interface PushEv extends WaitEvent {
  data: { json(): unknown } | null
}
interface ClickEv extends WaitEvent {
  notification: { close(): void; data?: { url?: unknown } }
}
interface FetchEv {
  request: Request
  respondWith(response: Promise<Response>): void
}

declare const self: WorkerScope
declare const __BUILD_HASH__: string
declare const __SHELL_FILES__: string[]

const PREFIX = 'wa-shell-'
const CACHE = PREFIX + __BUILD_HASH__

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(__SHELL_FILES__.map((url) => new Request(url, { cache: 'reload' }))))
      .then(() => self.skipWaiting()),
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith(PREFIX) && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  )
})

/** Same-origin URL or '/': rejects other origins and tricks like '/\\evil.com' (browsers read the backslash as a slash). */
function safeUrl(raw: unknown): string {
  if (typeof raw !== 'string') return '/'
  try {
    const parsed = new URL(raw, location.origin)
    return parsed.origin === location.origin ? parsed.pathname + parsed.search + parsed.hash : '/'
  } catch {
    return '/'
  }
}

// Payload: {title, body?, tag?, url?}. Without a title there is nothing to show, so it is ignored.
self.addEventListener('push', (event) => {
  let p: unknown
  try {
    p = event.data?.json() ?? null
  } catch {
    return
  }
  const fields = p as { title?: unknown; body?: unknown; tag?: unknown; url?: unknown } | null
  if (!fields || typeof fields.title !== 'string' || !fields.title) return
  const url = safeUrl(fields.url)
  event.waitUntil(
    self.registration.showNotification(fields.title, {
      body: typeof fields.body === 'string' ? fields.body : undefined,
      tag: typeof fields.tag === 'string' ? fields.tag : undefined,
      // A replaced notification (same tag) stays audible on Chrome and Android; renotify needs a tag.
      renotify: typeof fields.tag === 'string' && fields.tag !== '' ? true : undefined,
      data: { url },
    }),
  )
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const raw = event.notification.data?.url
  const url = safeUrl(raw)
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(async (all) => {
      const open = all[0]
      if (open) {
        try {
          await open.focus()
          await open.navigate(url)
        } catch {
          await self.clients.openWindow(url)
        }
      } else {
        await self.clients.openWindow(url)
      }
    }),
  )
})

self.addEventListener('fetch', (event) => {
  const { request } = event
  const url = new URL(request.url)
  if (request.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return
  if (request.mode === 'navigate') {
    // The app's own pages: network first (a new deploy shows at once), the cached shell when offline.
    event.respondWith(fetch(request).catch(async () => (await caches.match('/')) ?? Response.error()))
    return
  }
  // Shell files: cache first. Anything not in the shell cache goes straight to the network and is not stored.
  event.respondWith(caches.match(request).then((hit) => hit ?? fetch(request)))
})
