import type {
  BackendCfg,
  BackendInput,
  BackendsResponse,
  BackendTestResult,
  ChatAi,
  ChatList,
  FilterChip,
  DraftContext,
  DraftView,
  HistoryCoverage,
  HistoryDepth,
  HistoryStatus,
  SearchResponse,
} from './api-types'

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

/** The chat's current draft (or null) and the chat's canonical jid, which live events are keyed by. */
export async function fetchDraft(jid: string): Promise<{ draft: DraftView | null; chat_jid: string }> {
  return apiFetch<{ draft: DraftView | null; chat_jid: string }>(`${chatPath(jid)}/draft`)
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

export async function fetchDraftContext(): Promise<DraftContext> {
  return (await apiFetch<{ context: DraftContext }>('/api/ai/context')).context
}

/** Partial update: fields left out keep their saved value. */
export async function saveDraftContext(patch: Partial<DraftContext>): Promise<DraftContext> {
  return (
    await apiFetch<{ context: DraftContext }>('/api/ai/context', { method: 'PUT', body: JSON.stringify(patch) })
  ).context
}

export function fetchCoverage(): Promise<HistoryCoverage> {
  return apiFetch<HistoryCoverage>('/api/history/coverage')
}

export function fetchHistoryStatus(): Promise<HistoryStatus> {
  return apiFetch<HistoryStatus>('/api/history/status')
}

/** Queues older messages for the thin chats, or all of the most recent 50. */
export function fillHistory(scope: 'thin' | 'all', depth: HistoryDepth): Promise<{ added: number; status: HistoryStatus }> {
  return apiFetch(`/api/history/fill`, { method: 'POST', body: JSON.stringify({ scope, depth }) })
}

/** Queues older messages for one chat. Throws ApiError, with 404 only for unknown chats
 *  (and newsletters or broadcasts). A chat with nothing stored is still queued; it is skipped when its turn comes. */
export function loadChatHistory(jid: string, depth: HistoryDepth): Promise<{ added: number; status: HistoryStatus }> {
  return apiFetch(`${chatPath(jid)}/history`, { method: 'POST', body: JSON.stringify({ depth }) })
}

/** Drops what is queued; a chat already running finishes. */
export function cancelHistory(): Promise<HistoryStatus> {
  return apiFetch<HistoryStatus>('/api/history/cancel', { method: 'POST' })
}

export function fetchBackends(): Promise<BackendsResponse> {
  return apiFetch<BackendsResponse>('/api/ai/backends')
}

/** Creates a backend, or updates it when `id` is set. `api_key` omitted keeps the saved key; "" removes it. */
export async function saveBackend(input: BackendInput): Promise<BackendCfg> {
  return (
    await apiFetch<{ backend: BackendCfg }>('/api/ai/backends', {
      method: 'PUT',
      body: JSON.stringify(input),
    })
  ).backend
}

export function deleteBackend(id: number): Promise<unknown> {
  return apiFetch(`/api/ai/backends/${id}`, { method: 'DELETE' })
}

export function setDefaultBackend(id: number): Promise<unknown> {
  return apiFetch('/api/ai/default', {
    method: 'PUT',
    body: JSON.stringify({ backend_id: id }),
  })
}

/** Sends a fixed made-up two-line chat through the backend: never real chat content. */
export function testBackend(id: number): Promise<BackendTestResult> {
  return apiFetch<BackendTestResult>(`/api/ai/backends/${id}/test`, {
    method: 'POST',
  })
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
  replyChat?: string | null,
): Promise<SendResult> {
  const payload = {
    text,
    client_key: clientKey,
    ...(replyTo ? { reply_to: replyTo } : {}),
    // A private reply: the group the quoted message lives in (the chat being sent to is the sender's own).
    ...(replyTo && replyChat ? { reply_chat: replyChat } : {}),
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

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text)
  } catch {
    return null
  }
}

/** The most the server takes for one file (64 MiB). */
export const MAX_UPLOAD = 64 * 1024 * 1024

export type MediaUpload = {
  file: Blob
  filename: string
  caption: string | null
  replyTo: string | null
  /** The group a private reply quotes from. */
  replyChat?: string | null
  clientKey: string
  kind: 'file' | 'voice'
  /** 0 to 1, as the bytes leave this device. */
  onProgress?: (fraction: number) => void
}

/** Sends a photo, file or voice note as the raw body (XHR, for upload progress). `clientKey` makes a Retry safe. */
export function sendMedia(jid: string, up: MediaUpload): Promise<SendResult> {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `/api/chats/${encodeURIComponent(jid)}/media`)
    xhr.withCredentials = true
    xhr.setRequestHeader('Content-Type', up.file.type || 'application/octet-stream')
    xhr.setRequestHeader('X-Filename', encodeURIComponent(up.filename))
    xhr.setRequestHeader('X-Client-Key', up.clientKey)
    xhr.setRequestHeader('X-Kind', up.kind)
    if (up.caption) xhr.setRequestHeader('X-Caption', encodeURIComponent(up.caption))
    if (up.replyTo) xhr.setRequestHeader('X-Reply-To', up.replyTo)
    if (up.replyTo && up.replyChat) xhr.setRequestHeader('X-Reply-Chat', encodeURIComponent(up.replyChat))
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && e.total > 0) up.onProgress?.(e.loaded / e.total)
    }
    xhr.onerror = () => resolve(NO_ANSWER)
    xhr.ontimeout = () => resolve(NO_ANSWER)
    xhr.onload = () => {
      const body = parseJson(xhr.responseText)
      if (xhr.status === 401 && window.location.pathname !== '/login') navigation.toLogin()
      if (body && typeof body === 'object' && 'ok' in body) {
        if (body.ok === true) {
          const id = 'message_id' in body && typeof body.message_id === 'string' ? body.message_id : null
          resolve({ ok: true, message_id: id })
          return
        }
        if ('error' in body && typeof body.error === 'string') {
          resolve({ ok: false, error: body.error, retryable: 'retryable' in body && body.retryable === true })
          return
        }
      }
      const detail = body && typeof body === 'object' && 'error' in body ? String(body.error) : xhr.statusText
      resolve({ ok: false, error: `Not sent: ${detail}`, retryable: xhr.status >= 500 })
    }
    xhr.send(up.file)
  })
}

/** Message search, newest first, 30 a page. `chat` narrows it to one chat; `before` is the previous page's `next`. */
export function searchMessages(q: string, opts: { chat?: string; before?: string | null } = {}): Promise<SearchResponse> {
  const params = new URLSearchParams({ q })
  if (opts.chat) params.set('chat', opts.chat)
  if (opts.before) params.set('before', opts.before)
  return apiFetch<SearchResponse>(`/api/search?${params.toString()}`)
}

/** The chips above the chat list. Null when the server's answer is not a chip list (the app then shows its built-ins). */
export async function fetchChatFilters(): Promise<FilterChip[] | null> {
  const body = await apiFetch<{ filters?: FilterChip[] }>('/api/chat-filters')
  return Array.isArray(body?.filters) ? body.filters : null
}

/** The custom lists in saved order; with `chat`, each says whether that chat is in it. */
export async function fetchLists(chat?: string): Promise<ChatList[]> {
  const query = chat ? `?chat=${encodeURIComponent(chat)}` : ''
  return (await apiFetch<{ lists: ChatList[] }>(`/api/lists${query}`)).lists
}

export function createList(name: string): Promise<ChatList> {
  return apiFetch<ChatList>('/api/lists', { method: 'POST', body: JSON.stringify({ name }) })
}

export async function updateList(id: number, patch: { name?: string; position?: number }): Promise<void> {
  await apiFetch(`/api/lists/${id}`, { method: 'PUT', body: JSON.stringify(patch) })
}

export async function deleteList(id: number): Promise<void> {
  await apiFetch(`/api/lists/${id}`, { method: 'DELETE' })
}

export async function changeListChats(id: number, change: { add: string[]; remove: string[] }): Promise<void> {
  await apiFetch(`/api/lists/${id}/chats`, { method: 'PUT', body: JSON.stringify(change) })
}
