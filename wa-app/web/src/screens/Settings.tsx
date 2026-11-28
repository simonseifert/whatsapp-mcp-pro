import { useEffect, useState } from 'react'
import {
  apiFetch,
  deleteBackend,
  fetchBackends,
  fetchDraftContext,
  saveBackend,
  saveDraftContext,
  setDefaultBackend,
  testBackend,
} from '../api'
import type { BackendCfg, BackendInput, BackendTestResult, DraftContext } from '../api-types'
import { HistorySection } from '../components/HistorySection'
import { BackendForm } from '../components/BackendForm'
import { QuickRepliesSection } from '../components/QuickRepliesSection'
import { UpdateRow } from '../components/UpdateRow'
import { Lists } from './Lists'
import { disablePush, enablePush, pushState, pushSupport } from '../push'
import { readTheme, saveTheme, THEMES, type ThemePref } from '../theme'

const KIND_LABEL: Record<BackendCfg['kind'], string> = {
  claude_cli: 'Claude CLI',
  openai: 'OpenAI-compatible',
  anthropic: 'Anthropic API',
}

function secondsLine(ms: number): string {
  return `${(ms / 1000).toFixed(1)} s`
}

const MAX_ABOUT = 1500

/** What every draft knows: the About me note, and how much chat and how many past replies it reads. */
function DraftContextSection() {
  const [saved, setSaved] = useState<DraftContext | null>(null)
  const [form, setForm] = useState<DraftContext | null>(null)
  const [loadFailed, setLoadFailed] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    fetchDraftContext()
      .then((c) => {
        if (!live) return
        if (!c) throw new Error('empty')
        setSaved(c)
        setForm(c)
      })
      .catch(() => live && setLoadFailed(true))
    return () => {
      live = false
    }
  }, [])

  if (!saved || !form) {
    return loadFailed ? (
      <div className="settings-group">
        <div className="settings-label">Draft context</div>
        <p className="form-error">Couldn&apos;t load the draft context.</p>
      </div>
    ) : null
  }

  const changed: Partial<DraftContext> = {}
  if (form.about_me !== saved.about_me) changed.about_me = form.about_me
  if (form.context_messages !== saved.context_messages) changed.context_messages = form.context_messages
  if (form.own_examples !== saved.own_examples) changed.own_examples = form.own_examples
  const dirty = Object.keys(changed).length > 0

  async function save() {
    setSaving(true)
    setError(null)
    try {
      const next = await saveDraftContext(changed)
      setSaved(next)
      setForm(next)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't save")
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="settings-group">
      <div className="settings-label">Draft context</div>
      <div className="ai-form">
        <label className="ai-field">
          <span>About me</span>
          <textarea
            aria-label="About me"
            rows={4}
            maxLength={MAX_ABOUT}
            value={form.about_me}
            placeholder="Who you are and how you write. Used for every draft."
            onChange={(e) => setForm({ ...form, about_me: e.target.value })}
          />
          <span>{`${form.about_me.length}/${MAX_ABOUT}`}</span>
        </label>
        <label className="ai-field">
          <span>Messages a draft reads</span>
          <span className="ai-range">
            <input
              type="range"
              min={10}
              max={100}
              step={10}
              value={form.context_messages}
              onChange={(e) => setForm({ ...form, context_messages: Number(e.target.value) })}
            />
            <output>{form.context_messages}</output>
          </span>
        </label>
        <label className="ai-field">
          <span>Your past replies it learns from</span>
          <span className="ai-range">
            <input
              type="range"
              min={0}
              max={100}
              step={10}
              value={form.own_examples}
              onChange={(e) => setForm({ ...form, own_examples: Number(e.target.value) })}
            />
            <output>{form.own_examples}</output>
          </span>
        </label>
        <p className="settings-about">Higher values make drafts slower and can overflow small local models.</p>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div className="ai-form-actions">
          <button
            className="button-text"
            type="button"
            aria-label="Save draft context"
            disabled={!dirty || saving}
            onClick={() => void save()}
          >
            Save
          </button>
        </div>
      </div>
    </div>
  )
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
                        <button className="button-text neutral" type="button" onClick={() => setDeleting(null)}>
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

/** Notifications: the permission prompt only ever comes from tapping the switch, after the line that says why. */
function NotificationsSection() {
  const support = pushSupport()
  const [state, setState] = useState<'on' | 'off' | 'denied' | null>(null)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string | null>(null)

  useEffect(() => {
    if (support !== 'ok') return
    let live = true
    pushState()
      .then((s) => {
        if (!live) return
        if (s === 'not-ready') {
          setState('off')
          setNote("Notifications aren't ready yet. Close and reopen the app, then try again.")
        } else setState(s)
      })
      .catch(() => live && setState('off'))
    return () => {
      live = false
    }
  }, [support])

  async function toggle(next: boolean) {
    setBusy(true)
    setNote(null)
    try {
      if (next) {
        const result = await enablePush()
        setState(result === 'on' ? 'on' : result === 'denied' ? 'denied' : 'off')
        if (result === 'failed') setNote("Couldn't turn notifications on")
      } else {
        await disablePush()
        setState('off')
      }
    } catch {
      setNote("Couldn't change notifications")
    } finally {
      setBusy(false)
    }
  }

  async function sendTest() {
    setNote(null)
    try {
      const { sent } = await apiFetch<{ sent: number }>('/api/push/test', { method: 'POST' })
      setNote(sent > 0 ? 'Test sent' : 'No device accepted the test')
    } catch {
      setNote("Couldn't send the test")
    }
  }

  const disabled = support !== 'ok' || state === null || state === 'denied' || busy
  return (
    <div className="settings-group">
      <label className="sheet-row">
        <span>Notifications</span>
        <input
          type="checkbox"
          role="switch"
          aria-label="Notifications"
          checked={state === 'on'}
          disabled={disabled}
          onChange={(e) => void toggle(e.target.checked)}
        />
      </label>
      {support === 'install-first' && <p className="settings-about">Add wa-app to your Home Screen first</p>}
      {support === 'unsupported' && <p className="settings-about">This browser can&apos;t show notifications</p>}
      {support === 'ok' && state === 'off' && <p className="settings-about">Get a notification when a message arrives.</p>}
      {state === 'denied' && <p className="settings-about">Notifications are blocked in this browser&apos;s settings.</p>}
      {state === 'on' && (
        <div className="settings-actions">
          <button className="button-text" type="button" onClick={() => void sendTest()}>
            Send a test
          </button>
        </div>
      )}
      {note && <p className="settings-about" role="status">{note}</p>}
    </div>
  )
}

export function Settings({ onLogout }: { onLogout: (everywhere: boolean) => void }) {
  const [theme, setTheme] = useState<ThemePref>(readTheme)
  const [version, setVersion] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [showLists, setShowLists] = useState(false)

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

  if (showLists) return <Lists onBack={() => setShowLists(false)} />

  return (
    <div className="pane-scroll">
      <header className="lhead">
        <h1 className="large-title">Settings</h1>
      </header>
      <div className="settings">
        <div className="settings-group">
          <div className="settings-label">Chats</div>
          <button className="button-text neutral" type="button" onClick={() => setShowLists(true)}>
            Lists
          </button>
        </div>
        <QuickRepliesSection />
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
            <button className="button-text neutral" type="button" onClick={() => onLogout(false)}>
              Log out
            </button>
            {confirming ? (
              <>
                <p className="settings-about">This ends every signed-in session, on every device.</p>
                <button className="button-text danger" type="button" onClick={() => onLogout(true)}>
                  Yes, log out everywhere
                </button>
                <button className="button-text neutral" type="button" onClick={() => setConfirming(false)}>
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
        <NotificationsSection />
        <HistorySection />
        <AiSection />
        <DraftContextSection />
        <UpdateRow />
        {version && <p className="settings-about">wa-app {version}</p>}
      </div>
    </div>
  )
}
