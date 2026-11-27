import { apiFetch } from './api'

export type PushSupport = 'ok' | 'install-first' | 'unsupported'

const isIos = () => /iPhone|iPad|iPod/.test(navigator.userAgent)

function standalone(): boolean {
  return window.matchMedia('(display-mode: standalone)').matches || (navigator as { standalone?: boolean }).standalone === true
}

/** iOS only delivers pushes to a Home Screen app, and hides PushManager in a Safari tab: say "install first" there. */
export function pushSupport(): PushSupport {
  if (isIos() && !standalone()) return 'install-first'
  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) return 'unsupported'
  return 'ok'
}

function keyBytes(base64url: string): Uint8Array<ArrayBuffer> {
  const raw = atob(base64url.replace(/-/g, '+').replace(/_/g, '/'))
  return Uint8Array.from(raw, (c) => c.charCodeAt(0))
}

export const READY_TIMEOUT_MS = 5000

/** `serviceWorker.ready` never settles while the worker is still installing on a first open: give up after 5 s. */
function readyOrNull(): Promise<ServiceWorkerRegistration | null> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => resolve(null), READY_TIMEOUT_MS)
    navigator.serviceWorker.ready.then(
      (registration) => {
        clearTimeout(timer)
        resolve(registration)
      },
      (error: unknown) => {
        clearTimeout(timer)
        reject(error)
      },
    )
  })
}

/** Ask for permission (call from a tap only), subscribe and register the subscription with the server. */
export async function enablePush(): Promise<'on' | 'denied' | 'failed'> {
  let subscription: PushSubscription | null = null
  try {
    if ((await Notification.requestPermission()) !== 'granted') return 'denied'
    const { key } = await apiFetch<{ key: string }>('/api/push/key')
    const registration = await readyOrNull()
    if (!registration) return 'failed'
    subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: keyBytes(key),
    })
    await apiFetch('/api/push/subscribe', { method: 'POST', body: JSON.stringify(subscription.toJSON()) })
    return 'on'
  } catch {
    // The server never learned of this subscription: drop it, or the browser keeps an endpoint nobody sends to.
    await subscription?.unsubscribe().catch(() => undefined)
    return 'failed'
  }
}

export async function disablePush(): Promise<void> {
  const registration = await navigator.serviceWorker.ready
  const subscription = await registration.pushManager.getSubscription()
  if (!subscription) return
  const endpoint = subscription.endpoint
  await subscription.unsubscribe()
  await apiFetch('/api/push/unsubscribe', { method: 'POST', body: JSON.stringify({ endpoint }) }).catch(() => undefined)
}

/** 'not-ready': the service worker did not come up within 5 s (a first open); the caller shows a note. */
export async function pushState(): Promise<'on' | 'off' | 'denied' | 'not-ready'> {
  if (Notification.permission === 'denied') return 'denied'
  const registration = await readyOrNull()
  if (!registration) return 'not-ready'
  return (await registration.pushManager.getSubscription()) ? 'on' : 'off'
}
