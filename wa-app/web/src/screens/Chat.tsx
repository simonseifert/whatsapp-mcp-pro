import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router'
import type { DraftView, MessageView, QuoteView } from '../api-types'
import { Avatar } from '../components/Avatar'
import { Bubble } from '../components/Bubble'
import { ChatMenu } from '../components/ChatMenu'
import { ChatAiSheet } from '../components/ChatAiSheet'
import { AttachSheet } from '../components/AttachSheet'
import { Composer, type ComposerHandle } from '../components/Composer'
import { DraftCard } from '../components/DraftCard'
import { MessageMenu } from '../components/MessageMenu'
import { Icon } from '../components/Icon'
import { toItems } from '../chatItems'
import {
  clearNotice,
  dismiss,
  getBox,
  myReaction,
  pendingId,
  pendingView,
  quoteOf,
  react,
  reconcile,
  retry,
  sendFile,
  sendText,
  useOutbox,
  withReactions,
} from '../outbox'
import { useChat } from '../useChat'
import { useDraft } from '../useDraft'
import { useMarkRead } from '../useMarkRead'

const GROUP_KINDS = ['group', 'community_group', 'announcement']
const LOAD_OLDER_AT_PX = 160
const AT_BOTTOM_PX = 80 // within this of the end counts as "at the bottom"

/** A quoted message as a minimal message, so a draft's quote can become the composer's reply-to. */
function viewFromQuote(q: QuoteView, chatJid: string): MessageView {
  return {
    id: q.id,
    chat_jid: chatJid,
    sender_jid: null,
    sender_name: q.sender_name,
    from_me: false,
    timestamp_iso: new Date(0).toISOString(),
    text: q.preview,
    media: null,
    reply_to: null,
    forwarded: false,
    edited: false,
    deleted: false,
    system: false,
    reactions: [],
    poll: null,
    contact_cards: null,
    actionable: true,
    ticks: null,
  }
}

/** `active` is false while the pane slides out after the URL left the chat: nothing there may be marked read then. */
export function Chat({ jid, active = true }: { jid: string; active?: boolean }) {
  const scroller = useRef<HTMLDivElement>(null)
  const atBottom = useRef(true)
  const stickToEnd = useRef(false)
  const [riseKey, setRiseKey] = useState<string | null>(null)
  const [unseen, setUnseen] = useState(false)
  const [bottom, setBottom] = useState(true)

  // Only a message that is now the newest of the chat moves the view: it rises in when the reader is at the bottom,
  // otherwise a "New messages" pill offers the jump. History arriving above it, or anywhere older, does neither.
  const aliases = useRef<Record<string, string>>({})
  const onArrival = useCallback((message: MessageView, newest: boolean) => {
    if (!newest) return
    if (aliases.current[message.id]) return // the bubble I sent is already there: it takes over in place, no rise
    if (atBottom.current) {
      stickToEnd.current = true
      setRiseKey(`${message.chat_jid}/${message.id}`)
    } else setUnseen(true)
  }, [])
  const { detail, messages, hasMore, status, loadingOlder, loadOlder, retry: reload } = useChat(jid, onArrival)
  const box = useOutbox(jid)
  useEffect(() => {
    aliases.current = box.aliases
  }, [box.aliases])
  const [replyTo, setReplyTo] = useState<MessageView | null>(null)
  const [menuId, setMenuId] = useState<string | null>(null)
  const chatJid = detail?.chat.jid ?? jid
  const composer = useRef<ComposerHandle>(null)
  const { draft, error: draftError, request: requestDraft, dismiss: dismissDraft } = useDraft(jid)
  // The draft whose text is in the composer: the next send carries its id as a hint, until the box is emptied.
  // Its card is hidden (not dismissed) so the server can still learn from the outcome.
  const [usedDraft, setUsedDraft] = useState<number | null>(null)
  const usedNow = useRef<number | null>(null) // usedDraft, readable inside the same event as a send
  const [hiddenDraft, setHiddenDraft] = useState<number | null>(null)
  const [replacing, setReplacing] = useState<{ draft: DraftView; text: string } | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)
  const [aiOpen, setAiOpen] = useState(false)
  const [attached, setAttached] = useState<File | null>(null)
  const card = draft && draft.id !== hiddenDraft && ['pending', 'ready', 'failed'].includes(draft.status) ? draft : null
  useEffect(() => reconcile(jid, messages), [jid, messages])
  // The loaded messages with my optimistic reactions applied, then the texts still on their way out.
  const shown = useMemo(
    () => [
      ...messages.map((m) => withReactions(m, box.reactions[m.id])),
      // Once the real message is loaded it takes the optimistic bubble's place: never both (same key, one frame).
      ...box.pending
        .filter((p) => !(p.messageId && messages.some((m) => m.id === p.messageId)))
        .map((p) => pendingView(p, chatJid)),
    ],
    [messages, box.reactions, box.pending, chatJid],
  )
  const pendingByKey = new Map(box.pending.map((p) => [pendingId(p.key), p]))
  const menuMessage = menuId ? shown.find((m) => m.id === menuId) : undefined

  function send(text: string) {
    const reply = replyTo
    setReplyTo(null)
    const fromDraft = usedDraft
    usedNow.current = null
    setUsedDraft(null)
    void sendText(jid, text, reply ? quoteOf(reply) : null, fromDraft)
    const key = pendingId(getBox(jid).pending.at(-1)?.key ?? '')
    stickToEnd.current = true
    atBottom.current = true
    setRiseKey(key)
    setUnseen(false)
  }
  function sendAttachment(file: File, caption: string, quote: QuoteView | null, kind: 'file' | 'voice' = 'file') {
    setAttached(null)
    setReplyTo(null)
    void sendFile(jid, file, caption, quote, kind)
    const key = pendingId(getBox(jid).pending.at(-1)?.key ?? '')
    stickToEnd.current = true
    atBottom.current = true
    setRiseKey(key)
    setUnseen(false)
  }
  /** "Use" on a draft row: fills the box (and replies to the quoted message in groups). It never sends. */
  function applyDraft(d: DraftView, text: string) {
    setReplacing(null)
    usedNow.current = d.id
    setUsedDraft(d.id)
    setHiddenDraft(d.id)
    if (d.quote) setReplyTo(messages.find((m) => m.id === d.quote?.id) ?? viewFromQuote(d.quote, chatJid))
    composer.current?.fill(text)
  }
  function chooseDraft(d: DraftView, text: string) {
    if ((composer.current?.getText() ?? '').trim()) setReplacing({ draft: d, text })
    else applyDraft(d, text)
  }
  const anchor = useRef<{ height: number; top: number } | null>(null)
  const firstId = messages[0]?.id
  const lastId = messages[messages.length - 1]?.id
  const pendingCount = box.pending.length
  const settled = useRef(false)
  const prevFirstId = useRef<string | undefined>(undefined)
  const last = messages[messages.length - 1]
  useMarkRead({
    active,
    jid,
    ready: status === 'ready',
    newestKey: last ? `${last.chat_jid}/${last.id}` : undefined,
    upTo: last?.timestamp_iso,
    unread: detail?.chat.unread,
    markedUnread: detail?.chat.marked_unread ?? false,
    atBottom: bottom,
  })

  // Open at the newest message. When older messages arrive above (the first id changes), put the reader's view back
  // where it was: Safari has no native scroll anchoring. Not keyed on loadingOlder, which would consume the anchor
  // before the page has arrived.
  useLayoutEffect(() => {
    const el = scroller.current
    if (!el) return
    if (stickToEnd.current) {
      el.scrollTop = el.scrollHeight
      stickToEnd.current = false
    } else if (anchor.current && prevFirstId.current && firstId !== prevFirstId.current) {
      el.scrollTop = anchor.current.top + (el.scrollHeight - anchor.current.height)
      anchor.current = null
    } else if (!settled.current && lastId) {
      el.scrollTop = el.scrollHeight
      settled.current = true
    }
    prevFirstId.current = firstId
  }, [firstId, lastId, pendingCount])

  // A load that ended without new messages on top (a failed request, or a page of known messages) leaves no
  // reason to keep the anchor; a stale one would block every later load.
  useLayoutEffect(() => {
    if (!loadingOlder && anchor.current && firstId === prevFirstId.current) anchor.current = null
  }, [loadingOlder, firstId])

  async function onScroll() {
    const el = scroller.current
    if (!el) return
    atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight <= AT_BOTTOM_PX
    setBottom(atBottom.current)
    if (atBottom.current) setUnseen(false)
    if (!hasMore || loadingOlder || anchor.current || el.scrollTop > LOAD_OLDER_AT_PX) return
    anchor.current = { height: el.scrollHeight, top: el.scrollTop }
    // Nothing started, or the request failed: forget the anchor so the next scroll can try again.
    if (!(await loadOlder())) anchor.current = null
  }

  const chat = detail?.chat
  const inGroup = chat ? GROUP_KINDS.includes(chat.kind) : false
  return (
    <section className="chatpane" aria-label={chat?.name ?? 'Chat'}>
      <header className="chead">
        <Link className="iconbtn back-btn" to="/" aria-label="Back to chats">
          <Icon name="back" size={24} />
        </Link>
        {chat && <Avatar name={chat.name} src={chat.avatar_url} kind={chat.kind} />}
        <div className="who">
          <h1 className="gname" dir="auto">
            {chat?.name ?? ''}
          </h1>
          {detail?.members_line && <div className="mem">{detail.members_line}</div>}
        </div>
        {chat && (
          <button
            className="iconbtn"
            type="button"
            aria-label="Chat menu"
            aria-haspopup="dialog"
            onClick={() => setMenuOpen(true)}
          >
            <Icon name="more" size={22} />
          </button>
        )}
      </header>
      {loadingOlder && <p className="older">Loading older messages…</p>}
      {status === 'loading' && <p className="empty-state">Loading…</p>}
      {status === 'missing' && <p className="empty-state">This chat does not exist.</p>}
      {status === 'error' && (
        <div className="empty-state" role="alert">
          <p>Could not load this chat.</p>
          <button className="button-text" type="button" onClick={reload}>
            Try again
          </button>
        </div>
      )}
      {status === 'ready' && (
        <div className="msgs" ref={scroller} onScroll={() => void onScroll()}>
          {messages.length === 0 && <p className="empty-state">No messages yet.</p>}
          {toItems(shown, inGroup).map((item) => {
            if (item.kind === 'day')
              return (
                <div className="day" key={item.key}>
                  {item.label}
                </div>
              )
            if (item.kind === 'system')
              return (
                <div className="sysmsg" key={item.key} dir="auto">
                  {item.message.text}
                </div>
              )
            const m = item.message
            const pending = pendingByKey.get(m.id)
            // The real message that replaces my optimistic bubble keeps its key: same element, nothing flickers.
            const key = pending ? m.id : (box.aliases[m.id] ?? item.key)
            return (
              <Bubble
                key={key}
                message={m}
                showSender={item.showSender}
                groupAvatar={inGroup && !m.from_me}
                tail={item.tail}
                rise={key === riseKey}
                delivery={
                  pending && {
                    state: pending.status,
                    error: pending.error,
                    retryable: pending.retryable,
                    upload: pending.upload && {
                      name: pending.upload.name,
                      size: pending.upload.size,
                      progress: pending.upload.progress,
                    },
                  }
                }
                onRetry={pending && (() => void retry(jid, pending.key))}
                onDismiss={pending && (() => dismiss(jid, pending.key))}
                onReply={m.actionable ? () => setReplyTo(m) : undefined}
                onMenu={pending ? undefined : () => setMenuId(m.id)}
              />
            )
          })}
        </div>
      )}
      {attached && (
        <AttachSheet
          file={attached}
          replyTo={replyTo ? quoteOf(replyTo) : null}
          onSend={sendAttachment}
          onClose={() => setAttached(null)}
        />
      )}
      {menuOpen && (
        <ChatMenu
          onClose={() => setMenuOpen(false)}
          onAi={() => {
            setMenuOpen(false)
            setAiOpen(true)
          }}
        />
      )}
      {aiOpen && <ChatAiSheet jid={chatJid} onClose={() => setAiOpen(false)} />}
      {menuMessage && (
        <MessageMenu
          message={menuMessage}
          mine={myReaction(menuMessage, box.reactions[menuMessage.id])}
          onClose={() => setMenuId(null)}
          onReact={(emoji) => {
            setMenuId(null)
            void react(jid, menuMessage, emoji)
          }}
          onReply={() => {
            setMenuId(null)
            setReplyTo(menuMessage)
          }}
          onCopy={() => {
            setMenuId(null)
            void navigator.clipboard?.writeText(menuMessage.text ?? '')
          }}
        />
      )}
      {unseen && (
        <button
          className="newpill"
          type="button"
          onClick={() => {
            const el = scroller.current
            if (el) el.scrollTop = el.scrollHeight
            atBottom.current = true
            setBottom(true)
            setUnseen(false)
          }}
        >
          New messages
        </button>
      )}
      {box.notice && (
        <p className="notice" role="alert">
          <span dir="auto">{box.notice}</span>
          <button className="button-text" type="button" onClick={() => clearNotice(jid)}>
            Dismiss
          </button>
        </p>
      )}
      {status === 'ready' && card && (
        <div className="draft-slot">
          <DraftCard
            key={card.id}
            draft={card}
            name={chat?.name ?? ''}
            onUse={(text) => chooseDraft(card, text)}
            onDismiss={() => {
              usedNow.current = null
              setUsedDraft(null)
              setReplacing(null)
              dismissDraft()
            }}
            onRetry={() => void requestDraft(card.trigger_id)}
          />
          {replacing && replacing.draft.id === card.id && (
            <p className="draft-confirm" role="alertdialog" aria-label="Replace your text?">
              <span>Replace your text?</span>
              <button className="button-text" type="button" onClick={() => applyDraft(replacing.draft, replacing.text)}>
                Replace
              </button>
              <button className="button-text quiet" type="button" onClick={() => setReplacing(null)}>
                Keep mine
              </button>
            </p>
          )}
        </div>
      )}
      {draftError && (
        <p className="notice" role="alert">
          <span dir="auto">{draftError}</span>
        </p>
      )}
      {status === 'ready' && (
        <Composer
          ref={composer}
          replyTo={replyTo}
          onCancelReply={() => setReplyTo(null)}
          onSend={send}
          onAttach={setAttached}
          onVoice={(file) => sendAttachment(file, '', replyTo ? quoteOf(replyTo) : null, 'voice')}
          onTextChange={(t) => {
            if (t.trim() !== '') return
            // The box was emptied by hand (not by a send or a dismiss): the draft's card may come back.
            if (usedNow.current !== null) setHiddenDraft(null)
            usedNow.current = null
            setUsedDraft(null)
          }}
          onDraft={() => void requestDraft()}
          drafting={card?.status === 'pending'}
        />
      )}
    </section>
  )
}
