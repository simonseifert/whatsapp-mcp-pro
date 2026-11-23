export type ChatKind = 'private' | 'group' | 'community_group' | 'announcement' | 'newsletter'

export type LastMessage = {
  id: string
  sender_name: string | null
  from_me: boolean
  preview: string
  media_type: string | null
  deleted: boolean
  timestamp_iso: string
  ticks: 'sent' | 'delivered' | 'read' | null
}

export type ChatSummary = {
  jid: string
  name: string
  kind: ChatKind
  avatar_url: string
  last_message: LastMessage
  unread: number
  marked_unread: boolean
  pinned: boolean
  muted: boolean
  archived: boolean
  community_name: string | null
}

export type ChatsResponse = { chats: ChatSummary[]; archived_count: number; has_more: boolean }

export type MediaView = {
  type: string
  filename: string | null
  duration_s: number | null
  has_file: boolean
  /** Not stored yet, but the server can fetch it from the CDN on GET (never asks the phone). */
  downloadable: boolean
  size_bytes: number | null
}
export type QuoteView = { id: string; sender_name: string | null; preview: string }
export type ReactionView = { emoji: string; count: number; mine: boolean }
export type PollView = { question: string; options: { text: string; votes: number }[]; total: number }
export type ContactCardView = { name: string; phones: string[] }

export type MessageView = {
  id: string
  chat_jid: string
  sender_jid: string | null
  sender_name: string | null
  from_me: boolean
  timestamp_iso: string
  text: string | null
  media: MediaView | null
  reply_to: QuoteView | null
  forwarded: boolean
  edited: boolean
  deleted: boolean
  system: boolean
  reactions: ReactionView[]
  poll: PollView | null
  contact_cards: ContactCardView[] | null
  /** False for rows imported from a chat export: they have no WhatsApp message key. */
  actionable: boolean
  ticks: 'sent' | 'delivered' | 'read' | null
}

export type ChatDetail = { chat: ChatSummary; members_line: string | null }
export type MessagesResponse = { messages: MessageView[]; has_more: boolean }

/** A drafted reply the server prepared. Nothing in it is sent until the user taps Send. `quote` is set in groups. */
export type DraftView = {
  id: number
  chat_jid: string
  trigger_id: string
  status: 'pending' | 'ready' | 'failed' | 'dismissed' | 'superseded' | 'expired' | 'done'
  alternatives: string[]
  error: string | null
  quote: QuoteView | null
  created_at_iso: string
}

export type BackendCfg = {
  id: number
  kind: 'openai' | 'anthropic' | 'claude_cli'
  label: string
  base_url: string | null
  model: string
  has_key: boolean
  /** Decided by the server: on this machine or network, so a local-only chat may use it. */
  local: boolean
}
export type BackendInput = {
  id?: number
  kind: BackendCfg['kind']
  label: string
  base_url: string | null
  model: string
  /** Write-only. Omitted = keep the saved key, "" = remove it. */
  api_key?: string
}
export type BackendTestResult = { ok: true; ms: number; sample: string } | { ok: false; ms: number; error: string }
export type BackendsResponse = { backends: BackendCfg[]; default_id: number | null }

export type ChatAi = {
  chat_jid: string
  enabled: boolean
  backend_id: number | null
  local_only: boolean
  note: string | null
  rules: string | null
}

/** What /api/live pushes. `resync` means "refetch": the server could not replay what was missed (or the page has
 *  just come back to the foreground). */
export type LiveEvent =
  | { name: 'message'; data: { chat_jid: string; message: MessageView } }
  | { name: 'message_changed'; data: { chat_jid: string; id: string } }
  | { name: 'reaction'; data: { chat_jid: string; message_id: string } }
  | { name: 'chat'; data: { chat: ChatSummary } }
  | { name: 'draft'; data: { chat_jid: string; draft: DraftView | null } }
  | { name: 'resync'; data: Record<string, never> }
