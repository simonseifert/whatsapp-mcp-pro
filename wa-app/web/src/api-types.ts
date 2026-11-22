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

/** What /api/live pushes. `resync` means "refetch": the server could not replay what was missed (or the page has
 *  just come back to the foreground). */
export type LiveEvent =
  | { name: 'message'; data: { chat_jid: string; message: MessageView } }
  | { name: 'message_changed'; data: { chat_jid: string; id: string } }
  | { name: 'reaction'; data: { chat_jid: string; message_id: string } }
  | { name: 'chat'; data: { chat: ChatSummary } }
  | { name: 'resync'; data: Record<string, never> }
