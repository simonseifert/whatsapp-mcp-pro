import { useEffect, useState } from 'react'
import { fetchBackends, fetchCoverage, fillHistory } from '../api'
import { pushServerEnabled } from '../push'

export const DISMISSED_KEY = 'wa-app.getting-started.dismissed'
export const AI_SECTION_ID = 'settings-ai'

/** Null until the server answered; false means push is switched off server-side. */
export function usePushServer(): boolean | null {
  const [enabled, setEnabled] = useState<boolean | null>(null)
  useEffect(() => {
    let live = true
    void pushServerEnabled().then((ok) => live && setEnabled(ok))
    return () => {
      live = false
    }
  }, [])
  return enabled
}

/** Why push is off and how to fix it. Shown in the card and in place of the notifications switch. */
export function PushSetupHelp() {
  return (
    <p className="settings-about">
      Notifications are off on the server. Add <code>WA_APP_PUSH_SUBJECT=mailto:you@example.com</code> (or an https URL) to the .env
      file the app reads. <code>wactl setup</code> asks for it. Then restart the app with <code>wactl restart app</code>.
    </p>
  )
}

function readDismissed(): boolean {
  try {
    return localStorage.getItem(DISMISSED_KEY) === '1'
  } catch {
    return false
  }
}

/** First-run checklist at the top of Settings: one row per open item, gone once all are done or dismissed. */
export function GettingStarted({ pushOn }: { pushOn: boolean | null }) {
  const [dismissed, setDismissed] = useState(readDismissed)
  const [thin, setThin] = useState(0)
  const [noAi, setNoAi] = useState(false)
  const [busy, setBusy] = useState(false)
  const [started, setStarted] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (dismissed) return
    let live = true
    fetchCoverage()
      .then((c) => live && setThin(c.chats.filter((chat) => chat.thin).length))
      .catch(() => undefined) // a row we can't check stays out
    fetchBackends()
      .then((r) => live && setNoAi(r.backends.length === 0))
      .catch(() => undefined)
    return () => {
      live = false
    }
  }, [dismissed])

  const showPush = pushOn === false
  const showHistory = thin > 0
  if (dismissed || !(showPush || showHistory || noAi)) return null

  function dismiss() {
    try {
      localStorage.setItem(DISMISSED_KEY, '1')
    } catch {
      // Private mode: it hides for this visit only.
    }
    setDismissed(true)
  }

  async function fill() {
    setBusy(true)
    setError(null)
    try {
      await fillHistory('thin', '3y')
      setStarted(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't start")
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="settings-group" aria-label="Getting started">
      <div className="settings-label">Getting started</div>
      {showPush && (
        <div className="ai-form">
          <div className="ai-name">Turn on notifications</div>
          <PushSetupHelp />
        </div>
      )}
      {showHistory && (
        <div className="ai-form">
          <div className="ai-name">Load older messages</div>
          <p className="settings-about">
            {`${thin} ${thin === 1 ? 'chat has' : 'chats have'} too little of your writing for good drafts.`}
          </p>
          {started ? (
            <p className="settings-about">Started. Your phone will show a &apos;Finished syncing&apos; notice for each batch.</p>
          ) : (
            <div className="settings-actions">
              <button className="button-text" type="button" disabled={busy} onClick={() => void fill()}>
                Fill thin chats
              </button>
            </div>
          )}
          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
        </div>
      )}
      {noAi && (
        <div className="ai-form">
          <div className="ai-name">Set up AI drafts</div>
          <p className="settings-about">Add an AI to get drafted replies.</p>
          <div className="settings-actions">
            <button
              className="button-text"
              type="button"
              onClick={() => document.getElementById(AI_SECTION_ID)?.scrollIntoView?.({ behavior: 'smooth' })}
            >
              Go to AI settings
            </button>
          </div>
        </div>
      )}
      <div className="settings-actions">
        <button className="button-text neutral" type="button" aria-label="Dismiss getting started" onClick={dismiss}>
          Dismiss
        </button>
      </div>
    </section>
  )
}
