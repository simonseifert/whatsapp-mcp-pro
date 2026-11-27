import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchHistoryStatus } from '../api'
import type { HistoryDone, HistoryStatus } from '../api-types'

const POLL_MS = 5000

export const RESULT: Record<HistoryDone['result'], (detail: string) => string> = {
  done: (detail) => (detail ? `Done: ${detail}` : 'Done'),
  more: () => 'More available, run again',
  failed: (detail) => (detail ? `Failed: ${detail}` : 'Failed'),
  skipped: (detail) => (detail.startsWith('Paused') ? detail : 'Skipped: nothing to anchor on'),
}

/** The backfill queue's status: fetched on mount, then every 5 s while `isActive(status)` holds. Polling pauses while
 *  the page is hidden (and catches up when it is shown again) and stops on unmount. */
export function useHistoryStatus(isActive: (s: HistoryStatus) => boolean) {
  const [status, setStatus] = useState<HistoryStatus | null>(null)
  const mounted = useRef(true)

  const poll = useCallback(() => {
    fetchHistoryStatus()
      .then((s) => mounted.current && setStatus(s))
      .catch(() => undefined)
  }, [])

  useEffect(() => {
    mounted.current = true
    poll()
    return () => {
      mounted.current = false
    }
  }, [poll])

  const active = status !== null && isActive(status)

  useEffect(() => {
    if (!active) return
    const timer = setInterval(() => {
      if (!document.hidden) poll()
    }, POLL_MS)
    const onVisible = () => {
      if (!document.hidden) poll()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [active, poll])

  return { status, setStatus, active }
}
