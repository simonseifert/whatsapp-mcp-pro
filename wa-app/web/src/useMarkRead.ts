import { useEffect, useRef } from 'react'
import { markRead } from './api'

/** A live message that arrives in an open chat is marked read again after this long (a burst is one call). */
export const READ_DEBOUNCE_MS = 1000

type Input = {
  jid: string
  /** The chat is loaded and shows its newest message. */
  ready: boolean
  /** Identifies the newest message; it changes only when something new arrived. */
  newestKey: string | undefined
  /** Unread count the server reported for the chat; undefined until the chat detail has loaded. */
  unread: number | undefined
  /** The phone's "marked unread" dot is showing: opening the chat clears it even with nothing unread. */
  markedUnread: boolean
  /** The newest message is on screen (the reader is at the bottom). */
  atBottom: boolean
}

function pageVisible(): boolean {
  return document.visibilityState !== 'hidden'
}

/**
 * Marks the chat read once its newest message is on screen in a visible page, and again (debounced) when a newer one
 * arrives while that stays true. Never repeats for the same newest message. A chat opened with nothing unread is not
 * marked: nothing changes on the phone side, so there is nothing to send.
 */
export function useMarkRead({ jid, ready, newestKey, unread, markedUnread, atBottom }: Input): void {
  const done = useRef<string | null>(null) // the newest message already marked (or needing no marking)
  const visible = useRef(pageVisible())
  const wake = useRef<(() => void) | null>(null)

  useEffect(() => {
    const onChange = () => {
      visible.current = pageVisible()
      if (visible.current) wake.current?.()
    }
    document.addEventListener('visibilitychange', onChange)
    return () => document.removeEventListener('visibilitychange', onChange)
  }, [])

  useEffect(() => {
    done.current = null
  }, [jid])

  useEffect(() => {
    if (!ready || !newestKey || !atBottom || unread === undefined) return
    let timer: ReturnType<typeof setTimeout> | null = null
    const attempt = () => {
      if (!visible.current || done.current === newestKey) return
      const first = done.current === null
      if (first && unread === 0 && !markedUnread) {
        done.current = newestKey
        return
      }
      timer = setTimeout(
        () => {
          if (!pageVisible()) return // hidden meanwhile: the return to the page tries again
          const previous = done.current
          done.current = newestKey
          markRead(jid).catch(() => {
            // Not marked after all: the next arrival or a return to the page tries again.
            if (done.current === newestKey) done.current = previous
          })
        },
        first ? 0 : READ_DEBOUNCE_MS,
      )
    }
    wake.current = attempt
    attempt()
    return () => {
      wake.current = null
      if (timer) clearTimeout(timer)
    }
  }, [jid, ready, newestKey, unread, markedUnread, atBottom])
}
