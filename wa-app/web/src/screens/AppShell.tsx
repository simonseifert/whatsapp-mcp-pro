import { Route, Routes, useMatch } from 'react-router'
import { TabBar } from '../components/TabBar'
import { Chat } from './Chat'
import { ChatList } from './ChatList'
import { Placeholder } from './Placeholder'
import { Settings } from './Settings'

/** Phone: one pane (list, then chat) above the tab bar. From 900 px: the list on the left, the open chat on the right. */
export function AppShell({ onLogout }: { onLogout: () => void }) {
  const jid = useMatch('/chat/:jid')?.params.jid
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
        {jid ? <Chat key={jid} jid={jid} /> : <p className="select-chat">Select a chat</p>}
      </section>
    </div>
  )
}
