import { useEffect, useRef, useState } from 'react'
import { fetchBackends, fetchChatAi, saveChatAi } from '../api'
import type { BackendCfg, BackendsResponse, ChatAi } from '../api-types'
import { Icon } from './Icon'
import { useDialog } from './useDialog'

const MAX_NOTE = 500
const MAX_RULES = 2000
const NOT_LOCAL = 'Not local'

/** True for a loopback or private-network host (client-side approximation of the server's policy check). */
function isPrivateHost(host: string): boolean {
  const h = host.replace(/^\[|\]$/g, '').toLowerCase()
  if (h === 'localhost' || h === '::1' || h.endsWith('.localhost')) return true
  const m = /^(\d+)\.(\d+)\.\d+\.\d+$/.exec(h)
  if (!m) return false
  const [a, b] = [Number(m[1]), Number(m[2])]
  return a === 127 || a === 10 || (a === 192 && b === 168) || (a === 172 && b >= 16 && b <= 31)
}

/** Cloud = anything that is not an OpenAI-compatible server on a loopback or private address. The server decides for real. */
export function isCloud(b: Pick<BackendCfg, 'kind' | 'base_url'>): boolean {
  if (b.kind !== 'openai' || !b.base_url) return true
  try {
    return !isPrivateHost(new URL(b.base_url).hostname)
  } catch {
    return true
  }
}

type Form = { enabled: boolean; backend: string; localOnly: boolean; note: string; rules: string }

const toForm = (ai: ChatAi): Form => ({
  enabled: ai.enabled,
  backend: ai.backend_id == null ? '' : String(ai.backend_id),
  localOnly: ai.local_only,
  note: ai.note ?? '',
  rules: ai.rules ?? '',
})

/** The chat's AI settings: drafts on or off, which AI, local only, a note, and the rules learned from replies. */
export function ChatAiSheet({ jid, onClose }: { jid: string; onClose: () => void }) {
  const [form, setForm] = useState<Form | null>(null)
  const [backends, setBackends] = useState<BackendsResponse>({ backends: [], default_id: null })
  const [failed, setFailed] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  useEffect(() => {
    let current = true
    Promise.all([fetchChatAi(jid), fetchBackends()])
      .then(([ai, list]) => {
        if (!current) return
        setBackends(list)
        setForm(toForm(ai))
      })
      .catch(() => current && setFailed(true))
    return () => {
      current = false
    }
  }, [jid])

  const dialog = useRef<HTMLDivElement>(null)
  useDialog(dialog, onClose)

  const defaultBackend = backends.backends.find((b) => b.id === backends.default_id)
  const defaultCloud = defaultBackend ? isCloud(defaultBackend) : false

  const hasLocal = backends.backends.some((b) => !isCloud(b)) || (defaultBackend !== undefined && !defaultCloud)
  // Local only with a cloud (or no) backend would only fail when drafting: refuse to save it.
  const chosen = form ? backends.backends.find((b) => String(b.id) === form.backend) : undefined
  const cloudChoice = form?.localOnly === true && (chosen ? isCloud(chosen) : defaultCloud || !defaultBackend)

  function setLocalOnly(on: boolean) {
    setForm((f) => {
      if (!f) return f
      if (!on) return { ...f, localOnly: false }
      // A cloud choice cannot stay when the chat goes local only: fall back to Default, or the first local backend.
      const chosen = backends.backends.find((b) => String(b.id) === f.backend)
      if (!chosen || !isCloud(chosen)) return { ...f, localOnly: true }
      const local = backends.backends.find((b) => !isCloud(b))
      return { ...f, localOnly: true, backend: !defaultCloud ? '' : local ? String(local.id) : '' }
    })
  }

  async function save() {
    if (!form) return
    setSaving(true)
    setSaveError(null)
    try {
      await saveChatAi(jid, {
        enabled: form.enabled,
        backend_id: form.backend === '' ? null : Number(form.backend),
        local_only: form.localOnly,
        note: form.note,
        rules: form.rules,
      })
      onClose()
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Couldn't save")
      setSaving(false)
    }
  }

  return (
    <div className="menu-wrap sheet-wrap">
      <button className="menu-backdrop" type="button" aria-label="Close" onClick={onClose} tabIndex={-1} />
      <div className="sheet" role="dialog" aria-modal="true" aria-label="AI in this chat" ref={dialog}>
        <header className="sheet-head">
          <h2>
            <Icon name="sparkle" size={18} /> AI in this chat
          </h2>
          <button className="iconbtn" type="button" aria-label="Close" onClick={onClose}>
            <Icon name="close" size={20} />
          </button>
        </header>
        {failed && <p role="alert">Couldn&apos;t load the AI settings.</p>}
        {!form && !failed && <p>Loading…</p>}
        {form && (
          <>
            <label className="sheet-row">
              <span>Drafts in this chat</span>
              <input
                type="checkbox"
                role="switch"
                checked={form.enabled}
                onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
              />
            </label>
            <label className="sheet-row">
              <span>AI</span>
              <select value={form.backend} onChange={(e) => setForm({ ...form, backend: e.target.value })}>
                <option value="" disabled={form.localOnly && defaultCloud}>
                  {defaultBackend ? `Default (${defaultBackend.label})` : 'Default'}
                  {form.localOnly && defaultCloud ? ` · ${NOT_LOCAL}` : ''}
                </option>
                {backends.backends.map((b) => {
                  const blocked = form.localOnly && isCloud(b)
                  return (
                    <option key={b.id} value={String(b.id)} disabled={blocked}>
                      {b.label}
                      {blocked ? ` · ${NOT_LOCAL}` : ''}
                    </option>
                  )
                })}
              </select>
            </label>
            <label className="sheet-row">
              <span>Local only</span>
              <input
                type="checkbox"
                role="switch"
                checked={form.localOnly}
                onChange={(e) => setLocalOnly(e.target.checked)}
              />
            </label>
            <label className="sheet-field">
              <span>Note</span>
              <textarea
                rows={2}
                maxLength={MAX_NOTE}
                value={form.note}
                placeholder="Anything the AI should know about this chat"
                onChange={(e) => setForm({ ...form, note: e.target.value })}
              />
            </label>
            <label className="sheet-field">
              <span>Rules</span>
              <textarea
                rows={5}
                maxLength={MAX_RULES}
                value={form.rules}
                placeholder="Learned from your replies. Edit freely."
                onChange={(e) => setForm({ ...form, rules: e.target.value })}
              />
            </label>
            {form.localOnly && !hasLocal && <p role="alert">Add a local AI in Settings first</p>}
            {saveError && <p role="alert">{saveError}</p>}
            <button className="sheet-save" type="button" disabled={saving || cloudChoice} onClick={() => void save()}>
              Save
            </button>
          </>
        )}
      </div>
    </div>
  )
}
