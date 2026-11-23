import type { BackendsResponse, ChatAi, DraftView } from './api-types'

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export const navigation = { toLogin: () => window.location.assign('/login') }

/** JSON fetch against the app's own API. Throws ApiError; a 401 outside /login sends the user to /login. */
export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (init?.body !== undefined && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const response = await fetch(path, {
    ...init,
    headers,
    credentials: 'same-origin',
  })
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    if (response.status === 401 && window.location.pathname !== '/login') navigation.toLogin()
    const message = body && typeof body === 'object' && 'error' in body ? String(body.error) : response.statusText
    throw new ApiError(response.status, message)
  }
  return body as T
}

/** URL of a message's media file, its thumbnail, or (retry, POST only) the one that may ask the sender's phone. */
export function mediaUrl(chat: string, id: string, kind: 'file' | 'thumb' | 'retry' = 'file'): string {
  const base = `/api/media/${encodeURIComponent(chat)}/${encodeURIComponent(id)}`
  return kind === 'thumb' ? `${base}/thumb` : kind === 'retry' ? `${base}/retry` : base
}

const chatPath = (jid: string) => `/api/chats/${encodeURIComponent(jid)}`

/** The chat's current draft, or null. */
export async function fetchDraft(jid: string): Promise<DraftView | null> {
  return (await apiFetch<{ draft: DraftView | null }>(`${chatPath(jid)}/draft`)).draft
}

/** Asks for a draft: for the given message, or (no trigger) a one-off reply to the latest. Throws ApiError (404 when there is nothing to reply to). */
export async function requestDraft(jid: string, triggerId?: string): Promise<DraftView> {
  const body = await apiFetch<{ draft: DraftView }>(`${chatPath(jid)}/draft`, {
    method: 'POST',
    body: JSON.stringify(triggerId ? { trigger_id: triggerId } : {}),
  })
  return body.draft
}

export function dismissDraft(id: number): Promise<unknown> {
  return apiFetch(`/api/drafts/${id}`, { method: 'DELETE' })
}

export async function fetchChatAi(jid: string): Promise<ChatAi> {
  return (await apiFetch<{ ai: ChatAi }>(`${chatPath(jid)}/ai`)).ai
}

export async function saveChatAi(jid: string, patch: Partial<Omit<ChatAi, 'chat_jid'>>): Promise<ChatAi> {
  return (await apiFetch<{ ai: ChatAi }>(`${chatPath(jid)}/ai`, { method: 'PUT', body: JSON.stringify(patch) })).ai
}

export function fetchBackends(): Promise<BackendsResponse> {
  return apiFetch<BackendsResponse>('/api/ai/backends')
}

export type ReadResponse = { unread: number; receipts_sent: number }

/**
 * Marks the chat read here and on the phone's side (the server sends the read receipts). `upTo` is the timestamp of
 * the newest message the reader was actually shown: the server never marks anything newer.
 */
export function markRead(jid: string, upTo: string): Promise<ReadResponse> {
  return apiFetch<ReadResponse>(`/api/chats/${encodeURIComponent(jid)}/read`, {
    method: 'POST',
    body: JSON.stringify({ up_to: upTo }),
  })
}

/** What the send and react endpoints answer: success, or the fixed "Not sent: …" line and whether Retry may help. */
export type SendResult = { ok: true; message_id: string | null } | { ok: false; error: string; retryable: boolean }

const NO_ANSWER: SendResult = {
  ok: false,
  error: "Not sent: bridge didn't answer",
  retryable: true,
}

async function postOutward(path: string, payload: object): Promise<SendResult> {
  let response: Response
  try {
    response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      credentials: 'same-origin',
    })
  } catch {
    return NO_ANSWER // offline or dropped: the same client_key makes a retry safe
  }
  const body: unknown = await response.json().catch(() => null)
  if (response.status === 401 && window.location.pathname !== '/login') navigation.toLogin()
  if (body && typeof body === 'object' && 'ok' in body) {
    if (body.ok === true) {
      const id = 'message_id' in body && typeof body.message_id === 'string' ? body.message_id : null
      return { ok: true, message_id: id }
    }
    if ('error' in body && typeof body.error === 'string') {
      return {
        ok: false,
        error: body.error,
        retryable: 'retryable' in body && body.retryable === true,
      }
    }
  }
  const detail = body && typeof body === 'object' && 'error' in body ? String(body.error) : response.statusText
  return {
    ok: false,
    error: `Not sent: ${detail}`,
    retryable: response.status >= 500,
  }
}

/** Sends a text (optionally quoting a message, optionally from a draft). `clientKey` is the idempotency key: reuse it on Retry. */
export function sendMessage(
  jid: string,
  text: string,
  replyTo: string | null,
  clientKey: string,
  draftId?: number | null,
): Promise<SendResult> {
  const payload = {
    text,
    client_key: clientKey,
    ...(replyTo ? { reply_to: replyTo } : {}),
    // A hint for the server's outcome learning; it never changes what is sent.
    ...(draftId != null ? { draft_id: draftId } : {}),
  }
  return postOutward(`/api/chats/${encodeURIComponent(jid)}/send`, payload)
}

/** Reacts to a message; an empty emoji removes the reaction. */
export function sendReaction(jid: string, messageId: string, emoji: string, clientKey: string): Promise<SendResult> {
  return postOutward(`/api/chats/${encodeURIComponent(jid)}/react`, {
    message_id: messageId,
    emoji,
    client_key: clientKey,
  })
}
