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

/** Ask for permission (call from a tap only), subscribe and register the subscription with the server. */
export async function enablePush(): Promise<'on' | 'denied' | 'failed'> {
  try {
    if ((await Notification.requestPermission()) !== 'granted') return 'denied'
    const { key } = await apiFetch<{ key: string }>('/api/push/key')
    const registration = await navigator.serviceWorker.ready
    const subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: keyBytes(key),
    })
    await apiFetch('/api/push/subscribe', { method: 'POST', body: JSON.stringify(subscription.toJSON()) })
    return 'on'
  } catch {
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

export async function pushState(): Promise<'on' | 'off' | 'denied'> {
  if (Notification.permission === 'denied') return 'denied'
  const registration = await navigator.serviceWorker.ready
  return (await registration.pushManager.getSubscription()) ? 'on' : 'off'
}
