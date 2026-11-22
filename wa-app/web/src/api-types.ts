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

export type ChatsResponse = { chats: ChatSummary[]; archived_count: number }
