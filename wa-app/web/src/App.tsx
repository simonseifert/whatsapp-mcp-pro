import { useCallback, useEffect, useState } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router'
import { apiFetch } from './api'
import { Login } from './screens/Login'

type AuthState = { setup_needed: boolean; logged_in: boolean }

function Shell({ onLogout }: { onLogout: () => void }) {
  return (
    <main className="page">
      <header className="shell-header">
        <h1 className="large-title">Chats</h1>
        <button className="button-text" type="button" onClick={onLogout}>
          Log out
        </button>
      </header>
      <p className="empty">No chats yet.</p>
    </main>
  )
}

export default function App() {
  const [auth, setAuth] = useState<AuthState | null>(null)

  const refresh = useCallback(async () => {
    setAuth(await apiFetch<AuthState>('/api/auth/state'))
  }, [])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial load of server state
    void refresh()
  }, [refresh])

  async function logout() {
    await apiFetch('/api/auth/logout', { method: 'POST' })
    await refresh()
  }

  if (auth === null) return null
  return (
    <BrowserRouter>
      <Routes>
        <Route
          path="/login"
          element={auth.logged_in ? <Navigate to="/" replace /> : <Login setupNeeded={auth.setup_needed} onDone={refresh} />}
        />
        <Route path="/" element={auth.logged_in ? <Shell onLogout={logout} /> : <Navigate to="/login" replace />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
