import { useEffect, useState } from 'react'
import { apiFetch, deleteBackend, fetchBackends, saveBackend, setDefaultBackend, testBackend } from '../api'
import type { BackendCfg, BackendInput, BackendTestResult } from '../api-types'
import { BackendForm } from '../components/BackendForm'
import { UpdateRow } from '../components/UpdateRow'
import { readTheme, saveTheme, THEMES, type ThemePref } from '../theme'

const KIND_LABEL: Record<BackendCfg['kind'], string> = {
  claude_cli: 'Claude CLI',
  openai: 'OpenAI-compatible',
  anthropic: 'Anthropic API',
}

function secondsLine(ms: number): string {
  return `${(ms / 1000).toFixed(1)} s`
}

/** The AIs that write drafts: add, edit, test, pick the default, delete. Keys go in and never come back out. */
function AiSection() {
  const [backends, setBackends] = useState<BackendCfg[] | null>(null)
  const [defaultId, setDefaultId] = useState<number | null>(null)
  const [loadFailed, setLoadFailed] = useState(false)
  const [editing, setEditing] = useState<BackendCfg | 'new' | null>(null)
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [results, setResults] = useState<Record<number, BackendTestResult | 'running'>>({})
  const [deleting, setDeleting] = useState<number | null>(null)

  useEffect(() => {
    let live = true
    fetchBackends()
      .then((r) => {
        if (!live) return
        setBackends(r.backends)
        setDefaultId(r.default_id)
      })
      .catch(() => live && setLoadFailed(true))
    return () => {
      live = false
    }
  }, [])

  const fail = (err: unknown, fallback: string) => setError(err instanceof Error ? err.message : fallback)

  async function save(input: BackendInput) {
    setSaving(true)
    setFormError(null)
    try {
      const saved = await saveBackend(input)
      setBackends((list) => {
        const rest = list ?? []
        return rest.some((b) => b.id === saved.id) ? rest.map((b) => (b.id === saved.id ? saved : b)) : [...rest, saved]
      })
      setResults((r) => Object.fromEntries(Object.entries(r).filter(([id]) => Number(id) !== saved.id))) // an edit makes the last test stale
      setEditing(null)
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "Couldn't save")
    } finally {
      setSaving(false)
    }
  }

  async function makeDefault(id: number) {
    setError(null)
    try {
      await setDefaultBackend(id)
      setDefaultId(id)
    } catch (err) {
      fail(err, "Couldn't change the default")
    }
  }

  async function test(id: number) {
    setResults((r) => ({ ...r, [id]: 'running' }))
    try {
      const result = await testBackend(id)
      setResults((r) => ({ ...r, [id]: result }))
    } catch (err) {
      setResults((r) => ({
        ...r,
        [id]: {
          ok: false,
          ms: 0,
          error: err instanceof Error ? err.message : "Couldn't test",
        },
      }))
    }
  }

  async function remove(id: number) {
    setError(null)
    try {
      await deleteBackend(id)
      setBackends((list) => (list ?? []).filter((b) => b.id !== id))
      setDefaultId((d) => (d === id ? null : d))
      setDeleting(null)
    } catch (err) {
      fail(err, "Couldn't delete")
    }
  }

  return (
    <div className="settings-group">
      <div className="settings-label">AI</div>
      {loadFailed && <p className="form-error">Couldn&apos;t load your AIs.</p>}
      {backends?.length === 0 && editing === null && (
        <p className="settings-about">No AI yet. Add one to get drafted replies.</p>
      )}
      <ul className="ai-list">
        {(backends ?? []).map((b) => {
          const result = results[b.id]
          return (
            <li key={b.id} className="ai-item">
              {editing !== null && editing !== 'new' && editing.id === b.id ? (
                <BackendForm
                  backend={b}
                  saving={saving}
                  error={formError}
                  onSave={save}
                  onCancel={() => {
                    setEditing(null)
                    setFormError(null)
                  }}
                />
              ) : (
                <>
                  <div className="ai-row">
                    <input
                      type="radio"
                      name="ai-default"
                      aria-label={`Use ${b.label} by default`}
                      checked={defaultId === b.id}
                      onChange={() => void makeDefault(b.id)}
                    />
                    <div className="ai-main">
                      <div className="ai-name">{b.label}</div>
                      <div className="settings-about">
                        {KIND_LABEL[b.kind]} · {b.model}
                        {b.has_key && (
                          <>
                            {' '}
                            · <span>Key saved</span>
                          </>
                        )}
                      </div>
                    </div>
                  </div>
                  <div className="ai-actions">
                    <button
                      className="button-text"
                      type="button"
                      aria-label={`Test ${b.label}`}
                      disabled={result === 'running'}
                      onClick={() => void test(b.id)}
                    >
                      {result === 'running' ? 'Testing…' : 'Test'}
                    </button>
                    <button
                      className="button-text"
                      type="button"
                      aria-label={`Edit ${b.label}`}
                      onClick={() => {
                        setFormError(null)
                        setEditing(b)
                      }}
                    >
                      Edit
                    </button>
                    {deleting === b.id ? (
                      <>
                        <button className="button-text danger" type="button" onClick={() => void remove(b.id)}>
                          Yes, delete
                        </button>
                        <button className="button-text" type="button" onClick={() => setDeleting(null)}>
                          Cancel
                        </button>
                      </>
                    ) : (
                      <button
                        className="button-text danger"
                        type="button"
                        aria-label={`Delete ${b.label}`}
                        onClick={() => setDeleting(b.id)}
                      >
                        Delete
                      </button>
                    )}
                  </div>
                  {result && result !== 'running' && (
                    <div className="ai-result" role="status">
                      {result.ok ? (
                        <>
                          <div>Works · {secondsLine(result.ms)}</div>
                          <div className="settings-about">{result.sample}</div>
                        </>
                      ) : (
                        <div className="form-error">{result.error}</div>
                      )}
                    </div>
                  )}
                </>
              )}
            </li>
          )
        })}
      </ul>
      {error && <p className="form-error">{error}</p>}
      {editing === 'new' ? (
        <BackendForm
          saving={saving}
          error={formError}
          onSave={save}
          onCancel={() => {
            setEditing(null)
            setFormError(null)
          }}
        />
      ) : (
        <div className="settings-actions">
          <button
            className="button-text"
            type="button"
            onClick={() => {
              setFormError(null)
              setEditing('new')
            }}
          >
            Add an AI
          </button>
        </div>
      )}
    </div>
  )
}

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
        <AiSection />
        <UpdateRow />
        {version && <p className="settings-about">wa-app {version}</p>}
      </div>
    </div>
  )
}
