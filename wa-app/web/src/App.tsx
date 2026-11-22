import { useCallback, useEffect, useState } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router'
import { apiFetch } from './api'
import { AppShell } from './screens/AppShell'
import { Login } from './screens/Login'

type AuthState = { setup_needed: boolean; logged_in: boolean }

export default function App() {
  const [auth, setAuth] = useState<AuthState | null>(null)

  const refresh = useCallback(async () => {
    setAuth(await apiFetch<AuthState>('/api/auth/state'))
  }, [])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial load of server state
    void refresh()
  }, [refresh])

  async function logout(everywhere: boolean) {
    await apiFetch(everywhere ? '/api/auth/logout-all' : '/api/auth/logout', { method: 'POST' })
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
        <Route path="*" element={auth.logged_in ? <AppShell onLogout={logout} /> : <Navigate to="/login" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
