import { useCallback, useEffect, useRef, useState } from 'react'
import { cancelHistory, fetchCoverage, fillHistory } from '../api'
import type { HistoryCoverage, HistoryDepth } from '../api-types'
import { formatCount, formatMonth } from '../format'
import { RESULT, useHistoryStatus } from './useHistoryStatus'
const DEPTHS: { value: HistoryDepth; label: string }[] = [
  { value: 'year', label: 'Last year' },
  { value: '3y', label: '3 years' },
  { value: 'all', label: 'Everything' },
]

const plural = (n: number, one: string, many: string) => (n === 1 ? one : many)

/** What the app has stored per chat, and a one-tap way to ask the phone for older messages. */
export function HistorySection() {
  const [coverage, setCoverage] = useState<HistoryCoverage | null>(null)
  const [loadFailed, setLoadFailed] = useState(false)
  const [depth, setDepth] = useState<HistoryDepth>('3y')
  const [confirm, setConfirm] = useState<'thin' | 'all' | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const mounted = useRef(true)

  const loadCoverage = useCallback(() => {
    fetchCoverage()
      .then((c) => {
        if (!mounted.current) return
        setCoverage(c)
        setLoadFailed(false)
      })
      .catch(() => mounted.current && setLoadFailed(true))
  }, [])

  const { status, setStatus, active } = useHistoryStatus((st) => st.running !== null || st.queued.length > 0)

  useEffect(() => {
    mounted.current = true
    loadCoverage()
    return () => {
      mounted.current = false
    }
  }, [loadCoverage])

  // New messages arrived while it ran: refresh the numbers once it goes idle.
  const wasActive = useRef(false)
  useEffect(() => {
    if (wasActive.current && !active) loadCoverage()
    wasActive.current = active
  }, [active, loadCoverage])

  if (!coverage) {
    return loadFailed ? (
      <div className="settings-group">
        <div className="settings-label">History</div>
        <p className="form-error">Couldn&apos;t load the history coverage.</p>
      </div>
    ) : null
  }

  const names = new Map(coverage.chats.map((c) => [c.jid, c.name]))
  const nameOf = (jid: string) => names.get(jid) || jid.split('@')[0]
  const thin = coverage.chats.filter((c) => c.thin).length
  const oldest = formatMonth(coverage.totals.oldest)

  async function start(scope: 'thin' | 'all') {
    setBusy(true)
    setError(null)
    setNote(null)
    try {
      const res = await fillHistory(scope, depth)
      if (mounted.current) {
        setStatus(res.status)
        if (res.added === 0) setNote('Nothing to fill. Every chat is already queued or has enough history.')
      }
    } catch (err) {
      if (mounted.current) setError(err instanceof Error ? err.message : "Couldn't start")
    } finally {
      if (mounted.current) {
        setBusy(false)
        setConfirm(null)
      }
    }
  }

  async function stop() {
    try {
      const s = await cancelHistory()
      if (mounted.current) setStatus(s)
    } catch (err) {
      if (mounted.current) setError(err instanceof Error ? err.message : "Couldn't stop")
    }
  }

  const run = status?.running
  const waiting = status?.queued.length ?? 0

  return (
    <div className="settings-group">
      <div className="settings-label">History</div>
      <div className="ai-form">
        <p className="settings-about">
          {`${formatCount(coverage.totals.messages)} messages in ${formatCount(coverage.totals.chats)} chats${oldest ? `, oldest ${oldest}` : ''}`}
        </p>
        <p className="settings-about">
          {thin === 0
            ? 'Every chat has enough of your writing for good drafts'
            : `${thin} of your ${coverage.chats.length} most active ${plural(thin, 'chat has', 'chats have')} too little of your writing for good drafts`}
        </p>
        <ul className="history-list">
          {coverage.chats.map((c) => (
            <li key={c.jid}>
              <span className="history-name">{c.name || nameOf(c.jid)}</span>
              {c.thin && <span className="history-tag">Thin</span>}
              <span className="settings-about">
                {`${formatCount(c.messages)} messages, ${formatCount(c.own_texts)} from you, ${c.oldest ? `oldest ${formatMonth(c.oldest)}` : 'nothing stored'}`}
              </span>
            </li>
          ))}
        </ul>
        <fieldset className="settings-group">
          <legend>How far back</legend>
          <div className="segmented">
            {DEPTHS.map((d) => (
              <label key={d.value}>
                <input type="radio" name="history-depth" value={d.value} checked={depth === d.value} onChange={() => setDepth(d.value)} />
                {d.label}
              </label>
            ))}
          </div>
        </fieldset>
        {confirm ? (
          <div className="settings-actions">
            <p className="settings-about">
              Your phone will show a &apos;Finished syncing&apos; notice for each batch. Keep it online and charging.
            </p>
            <button className="button-text" type="button" disabled={busy} onClick={() => void start(confirm)}>
              Continue
            </button>
            <button className="button-text neutral" type="button" onClick={() => setConfirm(null)}>
              Cancel
            </button>
          </div>
        ) : (
          <div className="settings-actions">
            <button className="button-text" type="button" disabled={active} onClick={() => setConfirm('thin')}>
              Fill thin chats
            </button>
            <button className="button-text neutral" type="button" disabled={active} onClick={() => setConfirm('all')}>
              {`Fill all ${coverage.chats.length}`}
            </button>
          </div>
        )}
        {note && <p className="settings-about">{note}</p>}
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        {run && (
          <p className="settings-about">
            {`Filling ${nameOf(run.jid)}: ${run.batches} ${plural(run.batches, 'batch', 'batches')}${run.oldest ? `, back to ${formatMonth(run.oldest)}` : ''}`}
          </p>
        )}
        {waiting > 0 && <p className="settings-about">{`${waiting} waiting`}</p>}
        {active && (
          <div className="settings-actions">
            <button className="button-text danger" type="button" onClick={() => void stop()}>
              Stop
            </button>
          </div>
        )}
        {status && status.done.length > 0 && (
          <>
          <div className="settings-label">Last fill</div>
          <ul className="history-list" aria-label="Last fill results">
            {status.done.map((d, i) => (
              <li key={`${d.jid}-${i}`}>
                <span className="settings-about">{`${nameOf(d.jid)}: ${RESULT[d.result](d.detail)}`}</span>
              </li>
            ))}
          </ul>
          </>
        )}
      </div>
    </div>
  )
}
