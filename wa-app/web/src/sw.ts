// Service worker: caches the app shell only (index, JS, CSS, fonts, icons), versioned by the build.
// /api/* (chats, messages, media, the live stream) is never touched: the worker does not answer those at all.
// vite.config.ts replaces the two placeholders below at build time.

interface WorkerScope {
  skipWaiting(): Promise<void>
  clients: { claim(): Promise<void> }
  addEventListener(type: 'install' | 'activate', listener: (event: WaitEvent) => void): void
  addEventListener(type: 'fetch', listener: (event: FetchEv) => void): void
}
interface WaitEvent {
  waitUntil(work: Promise<unknown>): void
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
