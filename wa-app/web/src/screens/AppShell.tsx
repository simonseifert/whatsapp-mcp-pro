import { useEffect, useState } from 'react'
import { Route, Routes, useMatch } from 'react-router'
import { setOpenChat } from '../live'
import { TabBar } from '../components/TabBar'
import { UpdateRow } from '../components/UpdateRow'
import { Chat } from './Chat'
import { ChatList } from './ChatList'
import { Placeholder } from './Placeholder'
import { Settings } from './Settings'

/** Phone: one pane (list, then chat) above the tab bar. From 900 px: the list on the left, the open chat on the right. */
const SLIDE_MS = 280
const WIDE = '(min-width: 900px)'

/** The open chat's jid, kept for the length of the slide-out after the URL has left it, so the pane does not go blank. */
function useLeavingJid(jid: string | undefined): string | undefined {
  const [leaving, setLeaving] = useState<string>()
  const [last, setLast] = useState(jid)
  if (jid !== last) {
    setLast(jid)
    // Phones only: on desktop there is no slide, so the old chat goes at once.
    setLeaving(jid === undefined && !window.matchMedia(WIDE).matches ? last : undefined)
  }
  useEffect(() => {
    if (leaving === undefined) return
    const id = window.setTimeout(() => setLeaving(undefined), SLIDE_MS)
    return () => window.clearTimeout(id)
  }, [leaving])
  return leaving
}

export function AppShell({ onLogout }: { onLogout: (everywhere: boolean) => void }) {
  const jid = useMatch('/chat/:jid')?.params.jid
  const leaving = useLeavingJid(jid)
  const shown = jid ?? leaving
  // Only the route's chat counts as open, not the one sliding out.
  useEffect(() => setOpenChat(jid ?? null), [jid])
  useEffect(() => () => setOpenChat(null), []) // unmount only: switching A to B reports B, never a null in between
  return (
    <div className={jid ? 'shell has-chat' : 'shell'}>
      <section className="pane-list" aria-label="Navigation">
        <UpdateRow banner />
        <Routes>
          <Route path="/" element={<ChatList />} />
          <Route path="/archived" element={<ChatList archived />} />
          <Route path="/communities" element={<Placeholder title="Communities" />} />
          <Route path="/calls" element={<Placeholder title="Calls" />} />
          <Route path="/settings" element={<Settings onLogout={onLogout} />} />
          <Route path="*" element={<ChatList />} />
        </Routes>
        <TabBar />
      </section>
      <section className="pane-detail" aria-label="Chat">
        {shown ? <Chat key={shown} jid={shown} active={jid !== undefined} /> : <p className="select-chat">Select a chat</p>}
      </section>
    </div>
  )
}
