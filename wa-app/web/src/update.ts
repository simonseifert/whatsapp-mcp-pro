import { useSyncExternalStore } from 'react'

let available = false
const listeners = new Set<() => void>()

export function announceUpdate(): void {
  available = true
  listeners.forEach((l) => l())
}

/** Test helper. */
export function resetUpdate(): void {
  available = false
  listeners.forEach((l) => l())
}

export function useUpdateAvailable(): boolean {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l)
      return () => listeners.delete(l)
    },
    () => available,
  )
}

/**
 * A new worker took over a page that already had one: this page runs an old bundle, so say so. The very first
 * install also fires controllerchange, but there was no controller before it, so it is not announced.
 */
export function watchForUpdate(): void {
  const container = navigator.serviceWorker
  let had = container.controller !== null
  container.addEventListener('controllerchange', () => {
    if (had) announceUpdate()
    had = true
  })
}
