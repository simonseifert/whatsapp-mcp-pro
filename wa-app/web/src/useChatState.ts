import { useCallback, useState } from 'react'
import { setChatState, type ChatStatePatch } from './api'
import type { ChatSummary } from './api-types'

const flags = (c: ChatSummary | undefined) => (c ?  `${c.pinned}|${c.muted}|${c.archived}|${c.marked_unread}` : '')

/**
 * A chat with the changes the user just made laid over it. The server answers with the state the chat will have once
 * the phone's echo lands; until the list or the chat itself reports different flags than before, that answer is shown.
 */
export function useChatState<T extends ChatSummary | undefined>(chat: T) {
  const [applied, setApplied] = useState<{ base: string; chat: ChatSummary } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const base = flags(chat)
  const shown =
    chat && applied && applied.base === base
      ? {
          ...chat,
          pinned: applied.chat.pinned,
          muted: applied.chat.muted,
          archived: applied.chat.archived,
          marked_unread: applied.chat.marked_unread,
        }
      : chat
  const change = useCallback(
    async (patch: ChatStatePatch) => {
      if (!chat) return
      setError(null)
      const result = await setChatState(chat.jid, patch)
      if (result.ok) setApplied({ base, chat: result.chat })
      else setError(result.error)
    },
    [chat, base],
  )
  return { chat: shown, error, change, dismissError: () => setError(null) }
}
