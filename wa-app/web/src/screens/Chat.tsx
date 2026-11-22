import { useLayoutEffect, useRef } from 'react'
import { Link } from 'react-router'
import { Avatar } from '../components/Avatar'
import { Bubble } from '../components/Bubble'
import { Icon } from '../components/Icon'
import { toItems } from '../chatItems'
import { useChat } from '../useChat'

const GROUP_KINDS = ['group', 'community_group', 'announcement']
const LOAD_OLDER_AT_PX = 160

export function Chat({ jid }: { jid: string }) {
  const { detail, messages, hasMore, status, loadingOlder, loadOlder, retry } = useChat(jid)
  const scroller = useRef<HTMLDivElement>(null)
  const anchor = useRef<{ height: number; top: number } | null>(null)
  const firstId = messages[0]?.id
  const lastId = messages[messages.length - 1]?.id
  const settled = useRef(false)
  const prevFirstId = useRef<string | undefined>(undefined)

  // Open at the newest message. When older messages arrive above (the first id changes), put the reader's view back
  // where it was: Safari has no native scroll anchoring. Not keyed on loadingOlder, which would consume the anchor
  // before the page has arrived.
  useLayoutEffect(() => {
    const el = scroller.current
    if (!el) return
    if (anchor.current && prevFirstId.current && firstId !== prevFirstId.current) {
      el.scrollTop = anchor.current.top + (el.scrollHeight - anchor.current.height)
      anchor.current = null
    } else if (!settled.current && lastId) {
      el.scrollTop = el.scrollHeight
      settled.current = true
    }
    prevFirstId.current = firstId
  }, [firstId, lastId])

  // A load that ended without new messages on top (a failed request, or a page of known messages) leaves no
  // reason to keep the anchor; a stale one would block every later load.
  useLayoutEffect(() => {
    if (!loadingOlder && anchor.current && firstId === prevFirstId.current) anchor.current = null
  }, [loadingOlder, firstId])

  async function onScroll() {
    const el = scroller.current
    if (!el || !hasMore || loadingOlder || anchor.current || el.scrollTop > LOAD_OLDER_AT_PX) return
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
      </header>
      {loadingOlder && <p className="older">Loading older messages…</p>}
      {status === 'loading' && <p className="empty-state">Loading…</p>}
      {status === 'missing' && <p className="empty-state">This chat does not exist.</p>}
      {status === 'error' && (
        <div className="empty-state" role="alert">
          <p>Could not load this chat.</p>
          <button className="button-text" type="button" onClick={retry}>
            Try again
          </button>
        </div>
      )}
      {status === 'ready' && (
        <div className="msgs" ref={scroller} onScroll={() => void onScroll()}>
          {messages.length === 0 && <p className="empty-state">No messages yet.</p>}
          {toItems(messages, inGroup).map((item) =>
            item.kind === 'day' ? (
              <div className="day" key={item.key}>
                {item.label}
              </div>
            ) : item.kind === 'system' ? (
              <div className="sysmsg" key={item.key} dir="auto">
                {item.message.text}
              </div>
            ) : (
              <Bubble key={item.key} message={item.message} showSender={item.showSender} tail={item.tail} />
            ),
          )}
        </div>
      )}
    </section>
  )
}
