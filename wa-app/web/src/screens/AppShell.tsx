import { useEffect, useState } from 'react'
import { Route, Routes, useMatch } from 'react-router'
import { TabBar } from '../components/TabBar'
import { Chat } from './Chat'
import { ChatList } from './ChatList'
import { Placeholder } from './Placeholder'
import { Settings } from './Settings'

/** Phone: one pane (list, then chat) above the tab bar. From 900 px: the list on the left, the open chat on the right. */
const SLIDE_MS = 280

/** The open chat's jid, kept for the length of the slide-out after the URL has left it, so the pane does not go blank. */
function useLeavingJid(jid: string | undefined): string | undefined {
  const [leaving, setLeaving] = useState<string>()
  const [last, setLast] = useState(jid)
  if (jid !== last) {
    setLast(jid)
    setLeaving(jid === undefined ? last : undefined)
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
  return (
    <div className={jid ? 'shell has-chat' : 'shell'}>
      <section className="pane-list" aria-label="Navigation">
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
        {shown ? <Chat key={shown} jid={shown} /> : <p className="select-chat">Select a chat</p>}
      </section>
    </div>
  )
}
