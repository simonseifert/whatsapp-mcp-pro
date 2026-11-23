import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, dismissDraft, fetchDraft, requestDraft } from './api'
import type { DraftView } from './api-types'
import { subscribeLive } from './live'

/** How long the "nothing to reply to" style error stays on screen. */
export const ERROR_MS = 4000

/**
 * The one source of draft state for a chat: the current draft (initial GET, then the `draft` live events, where null
 * removes it), `request()` for a draft on demand, `dismiss()` to drop it. A draft is only ever text to put in the
 * composer; nothing here sends anything.
 */
export function useDraft(chatJid: string) {
  // Tagged with the chat it belongs to, so a card never outlives a switch to another chat.
  const [held, setHeld] = useState<{ jid: string; draft: DraftView | null }>({ jid: chatJid, draft: null })
  const draft = held.jid === chatJid ? held.draft : null
  const setDraft = useCallback(
    (update: DraftView | null | ((d: DraftView | null) => DraftView | null)) =>
      setHeld((h) => {
        const base = h.jid === chatJid ? h.draft : null
        return { jid: chatJid, draft: typeof update === 'function' ? update(base) : update }
      }),
    [chatJid],
  )
  const latest = useRef(draft)
  useEffect(() => {
    latest.current = draft
  })
  const [error, setError] = useState<string | null>(null)
  // The route may name the chat differently from the server's canonical jid; events carry the canonical one.
  const canonical = useRef<string | null>(null)
  const errorTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    let current = true
    canonical.current = null
    fetchDraft(chatJid)
      .then((d) => {
        if (!current) return
        canonical.current = d?.chat_jid ?? null
        setDraft((prev) => prev ?? d) // an event that raced the GET is newer
      })
      .catch(() => {}) // no card is the safe fallback; the next event or resync fills it
    const unsubscribe = subscribeLive((event) => {
      if (event.name === 'resync') {
        void fetchDraft(chatJid)
          .then((d) => current && setDraft(d))
          .catch(() => {})
        return
      }
      if (event.name !== 'draft') return
      const { chat_jid, draft: next } = event.data
      if (chat_jid !== chatJid && chat_jid !== canonical.current) return
      if (next) canonical.current = next.chat_jid
      setDraft(next)
    })
    return () => {
      current = false
      unsubscribe()
      if (errorTimer.current) clearTimeout(errorTimer.current)
    }
  }, [chatJid, setDraft])

  const request = useCallback(
    async (triggerId?: string) => {
      try {
        const next = await requestDraft(chatJid, triggerId)
        canonical.current = next.chat_jid
        setError(null)
        setDraft(next)
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Couldn't draft")
        if (errorTimer.current) clearTimeout(errorTimer.current)
        errorTimer.current = setTimeout(() => setError(null), ERROR_MS)
      }
    },
    [chatJid, setDraft],
  )

  const dismiss = useCallback(() => {
    const d = latest.current
    if (d) void dismissDraft(d.id).catch(() => {})
    setDraft(null)
  }, [setDraft])

  return { draft, error, request, dismiss }
}
