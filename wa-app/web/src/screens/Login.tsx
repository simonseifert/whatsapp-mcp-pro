import { useState, type FormEvent } from 'react'
import { ApiError, apiFetch } from '../api'

const MIN_LENGTH = 10

type Props = { setupNeeded: boolean; onDone: () => void }

export function Login({ setupNeeded, onDone }: Props) {
  const [passphrase, setPassphrase] = useState('')
  const [repeat, setRepeat] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    if (setupNeeded) {
      if (passphrase.length < MIN_LENGTH) return setError(`Use at least ${MIN_LENGTH} characters`)
      if (passphrase !== repeat) return setError('The passphrases do not match')
    }
    setBusy(true)
    try {
      if (setupNeeded) await apiFetch('/api/auth/setup', { method: 'POST', body: JSON.stringify({ passphrase }) })
      await apiFetch('/api/auth/login', { method: 'POST', body: JSON.stringify({ passphrase }) })
      onDone()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not reach the server')
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="page">
      <h1 className="large-title">wa-app</h1>
      <p className="secondary">{setupNeeded ? 'Choose a passphrase to protect this app.' : 'Enter your passphrase.'}</p>
      <form className="form" onSubmit={submit}>
        <input
          className="field"
          type="password"
          aria-label="Passphrase"
          placeholder="Passphrase"
          autoComplete={setupNeeded ? 'new-password' : 'current-password'}
          value={passphrase}
          onChange={(e) => setPassphrase(e.target.value)}
        />
        {setupNeeded && (
          <input
            className="field"
            type="password"
            aria-label="Repeat passphrase"
            placeholder="Repeat passphrase"
            autoComplete="new-password"
            value={repeat}
            onChange={(e) => setRepeat(e.target.value)}
          />
        )}
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <button className="button-primary" type="submit" disabled={busy || passphrase === ''}>
          {setupNeeded ? 'Set passphrase' : 'Log in'}
        </button>
      </form>
    </main>
  )
}
