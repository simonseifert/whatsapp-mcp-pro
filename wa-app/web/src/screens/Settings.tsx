import { useEffect, useState } from 'react'
import { apiFetch } from '../api'
import { UpdateRow } from '../components/UpdateRow'
import { readTheme, saveTheme, THEMES, type ThemePref } from '../theme'

export function Settings({ onLogout }: { onLogout: (everywhere: boolean) => void }) {
  const [theme, setTheme] = useState<ThemePref>(readTheme)
  const [version, setVersion] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)

  useEffect(() => {
    let live = true
    apiFetch<{ version: string }>('/api/about')
      .then((about) => live && setVersion(about.version))
      .catch(() => undefined) // the version line is a courtesy; it stays out when the server does not answer
    return () => {
      live = false
    }
  }, [])

  function choose(next: ThemePref) {
    setTheme(next)
    saveTheme(next)
  }

  return (
    <div className="pane-scroll">
      <header className="lhead">
        <h1 className="large-title">Settings</h1>
      </header>
      <div className="settings">
        <fieldset className="settings-group">
          <legend>Theme</legend>
          <div className="segmented">
            {THEMES.map((t) => (
              <label key={t.value}>
                <input type="radio" name="theme" value={t.value} checked={theme === t.value} onChange={() => choose(t.value)} />
                {t.label}
              </label>
            ))}
          </div>
        </fieldset>
        <div className="settings-group">
          <div className="settings-label">Session</div>
          <div className="settings-actions">
            <button className="button-text" type="button" onClick={() => onLogout(false)}>
              Log out
            </button>
            {confirming ? (
              <>
                <p className="settings-about">This ends every signed-in session, on every device.</p>
                <button className="button-text danger" type="button" onClick={() => onLogout(true)}>
                  Yes, log out everywhere
                </button>
                <button className="button-text" type="button" onClick={() => setConfirming(false)}>
                  Cancel
                </button>
              </>
            ) : (
              <button className="button-text danger" type="button" onClick={() => setConfirming(true)}>
                Log out everywhere
              </button>
            )}
          </div>
        </div>
        <UpdateRow />
        {version && <p className="settings-about">wa-app {version}</p>}
      </div>
    </div>
  )
}
