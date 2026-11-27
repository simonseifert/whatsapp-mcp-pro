import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { disablePush, enablePush, pushSupport, pushState, READY_TIMEOUT_MS, resetResync, resyncPush } from './push'

const IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148'
const original = { ua: navigator.userAgent, matchMedia: window.matchMedia }

function setUa(ua: string) {
  Object.defineProperty(navigator, 'userAgent', { value: ua, configurable: true })
}

function mockStandalone(on: boolean) {
  window.matchMedia = ((q: string) => ({ matches: on && q.includes('standalone') })) as never
}

function mockPush(subscription: unknown = null) {
  const unsubscribe = vi.fn().mockResolvedValue(true)
  const pushManager = {
    subscribe: vi.fn().mockResolvedValue({ unsubscribe, toJSON: () => ({ endpoint: 'https://web.push.apple.com/x', keys: { p256dh: 'p', auth: 'a' } }) }),
    getSubscription: vi.fn().mockResolvedValue(subscription),
  }
  Object.defineProperty(navigator, 'serviceWorker', { value: { ready: Promise.resolve({ pushManager }) }, configurable: true })
  vi.stubGlobal('PushManager', class {})
  ;(window as unknown as Record<string, unknown>).PushManager = class {}
  return Object.assign(pushManager, { unsubscribe })
}

beforeEach(() => {
  mockStandalone(false)
})

afterEach(() => {
  setUa(original.ua)
  window.matchMedia = original.matchMedia
  delete (window as unknown as Record<string, unknown>).PushManager
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('pushSupport', () => {
  it('asks iOS users to install first when PushManager exists but the app is not standalone', () => {
    mockPush()
    setUa(IPHONE)
    expect(pushSupport()).toBe('install-first')
  })

  it('asks for the install on iOS even when Safari hides PushManager', () => {
    setUa(IPHONE)
    expect(pushSupport()).toBe('install-first')
  })

  it('is ok in a standalone iOS app', () => {
    mockPush()
    mockStandalone(true)
    vi.stubGlobal('Notification', { permission: 'default' })
    setUa(IPHONE)
    expect(pushSupport()).toBe('ok')
  })

  it('is unsupported without PushManager elsewhere', () => {
    expect(pushSupport()).toBe('unsupported')
  })
})

describe('enablePush', () => {
  it('returns denied and posts nothing when permission is refused', async () => {
    const manager = mockPush()
    vi.stubGlobal('Notification', { permission: 'default', requestPermission: vi.fn().mockResolvedValue('denied') })
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    expect(await enablePush()).toBe('denied')
    expect(fetchMock).not.toHaveBeenCalled()
    expect(manager.subscribe).not.toHaveBeenCalled()
  })

  it('subscribes with the server key and registers the subscription', async () => {
    const manager = mockPush()
    vi.stubGlobal('Notification', { permission: 'default', requestPermission: vi.fn().mockResolvedValue('granted') })
    const fetchMock = vi.fn().mockImplementation(async (path: string) =>
      new Response(JSON.stringify(path === '/api/push/key' ? { key: 'AQID' } : { ok: true }), { status: 200 }),
    )
    vi.stubGlobal('fetch', fetchMock)
    expect(await enablePush()).toBe('on')
    expect(manager.subscribe).toHaveBeenCalledWith({ userVisibleOnly: true, applicationServerKey: new Uint8Array([1, 2, 3]) })
    const post = fetchMock.mock.calls.find(([p]) => p === '/api/push/subscribe')
    expect(JSON.parse(post![1].body).endpoint).toBe('https://web.push.apple.com/x')
  })

  it('unsubscribes the browser again when registering with the server fails', async () => {
    const manager = mockPush()
    vi.stubGlobal('Notification', { permission: 'default', requestPermission: vi.fn().mockResolvedValue('granted') })
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async (path: string) =>
        path === '/api/push/key'
          ? new Response(JSON.stringify({ key: 'AQID' }), { status: 200 })
          : new Response(JSON.stringify({ error: 'nope' }), { status: 422 }),
      ),
    )
    expect(await enablePush()).toBe('failed')
    expect(manager.unsubscribe).toHaveBeenCalledOnce()
  })

  it('fails after 5 s when the service worker never gets ready', async () => {
    vi.useFakeTimers()
    Object.defineProperty(navigator, 'serviceWorker', { value: { ready: new Promise(() => {}) }, configurable: true })
    vi.stubGlobal('Notification', { permission: 'default', requestPermission: vi.fn().mockResolvedValue('granted') })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ key: 'AQID' }), { status: 200 })))
    const result = enablePush()
    await vi.advanceTimersByTimeAsync(READY_TIMEOUT_MS)
    expect(await result).toBe('failed')
    vi.useRealTimers()
  })

  it('returns failed when the server has no push key', async () => {
    mockPush()
    vi.stubGlobal('Notification', { permission: 'default', requestPermission: vi.fn().mockResolvedValue('granted') })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: 'Push not configured' }), { status: 503 })))
    expect(await enablePush()).toBe('failed')
  })
})

describe('pushState and disablePush', () => {
  it('reports denied, on and off', async () => {
    vi.stubGlobal('Notification', { permission: 'denied' })
    expect(await pushState()).toBe('denied')
    vi.stubGlobal('Notification', { permission: 'granted' })
    mockPush({ endpoint: 'e' })
    expect(await pushState()).toBe('on')
    mockPush(null)
    expect(await pushState()).toBe('off')
  })

  it('reports not-ready after 5 s when the service worker never gets ready', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('Notification', { permission: 'default' })
    Object.defineProperty(navigator, 'serviceWorker', { value: { ready: new Promise(() => {}) }, configurable: true })
    const state = pushState()
    let settled = false
    void state.then(() => (settled = true))
    await vi.advanceTimersByTimeAsync(READY_TIMEOUT_MS - 1)
    expect(settled).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(await state).toBe('not-ready')
    vi.useRealTimers()
  })

  it('unsubscribes locally and tells the server', async () => {
    const unsubscribe = vi.fn().mockResolvedValue(true)
    mockPush({ endpoint: 'https://fcm.googleapis.com/z', unsubscribe })
    const fetchMock = vi.fn().mockResolvedValue(new Response('{"ok":true}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await disablePush()
    expect(unsubscribe).toHaveBeenCalled()
    expect(fetchMock.mock.calls[0][0]).toBe('/api/push/unsubscribe')
  })
})

describe('resyncPush', () => {
  beforeEach(resetResync)

  it('re-registers an existing subscription once per load', async () => {
    mockPush({ endpoint: 'https://web.push.apple.com/x', toJSON: () => ({ endpoint: 'https://web.push.apple.com/x', keys: { p256dh: 'p', auth: 'a' } }) })
    vi.stubGlobal('Notification', { permission: 'granted' })
    const fetchMock = vi.fn().mockResolvedValue(new Response('{"ok":true}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await resyncPush()
    await resyncPush()
    const posts = fetchMock.mock.calls.filter(([p]) => p === '/api/push/subscribe')
    expect(posts).toHaveLength(1)
    expect(JSON.parse(posts[0][1].body).endpoint).toBe('https://web.push.apple.com/x')
  })

  it('does nothing without a subscription or permission, and swallows failures', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error('offline'))
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('Notification', { permission: 'granted' })
    mockPush(null)
    await resyncPush()
    resetResync()
    vi.stubGlobal('Notification', { permission: 'default' })
    mockPush({ endpoint: 'e', toJSON: () => ({ endpoint: 'e' }) })
    await resyncPush()
    expect(fetchMock).not.toHaveBeenCalled()
    resetResync()
    vi.stubGlobal('Notification', { permission: 'granted' })
    await expect(resyncPush()).resolves.toBeUndefined()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
