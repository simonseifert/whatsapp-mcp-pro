import { useState } from 'react'
import type { BackendCfg, BackendInput } from '../api-types'

type Preset = {
  name: string
  kind: BackendCfg['kind']
  baseUrl: string | null
  model: string
}

export const PRESETS: Preset[] = [
  {
    name: 'Claude (claude CLI)',
    kind: 'claude_cli',
    baseUrl: null,
    model: 'sonnet',
  },
  {
    name: 'Local (Ollama)',
    kind: 'openai',
    baseUrl: 'http://127.0.0.1:11434/v1',
    model: '',
  },
  {
    name: 'Anthropic API',
    kind: 'anthropic',
    baseUrl: null,
    model: 'claude-sonnet-5-5',
  },
  { name: 'OpenAI-compatible', kind: 'openai', baseUrl: '', model: '' },
]

function presetFor(b?: BackendCfg): Preset {
  if (!b) return PRESETS[0]!
  const ofKind = PRESETS.filter((p) => p.kind === b.kind)
  return ofKind.find((p) => p.baseUrl === b.base_url) ?? ofKind[ofKind.length - 1]!
}

type Props = {
  /** Set when editing; its key is never shown. */
  backend?: BackendCfg
  onSave: (input: BackendInput) => Promise<void> | void
  onCancel: () => void
  error?: string | null
  saving?: boolean
}

/** Add or edit one AI. The key field is write-only: empty = keep what is saved, Remove key = clear it. */
export function BackendForm({ backend, onSave, onCancel, error, saving }: Props) {
  const [preset, setPreset] = useState<Preset>(() => presetFor(backend))
  const [label, setLabel] = useState(backend?.label ?? preset.name)
  const [baseUrl, setBaseUrl] = useState(backend?.base_url ?? preset.baseUrl ?? '')
  const [model, setModel] = useState(backend?.model ?? preset.model)
  const [key, setKey] = useState('')
  const [removeKey, setRemoveKey] = useState(false)
  const [invalid, setInvalid] = useState(false)

  const cli = preset.kind === 'claude_cli'
  const hasKey = !!backend?.has_key && !removeKey
  const moved =
    !!backend &&
    (preset.kind !== backend.kind || (preset.kind === 'openai' ? baseUrl.trim() : null) !== (backend.base_url ?? null))

  function choose(name: string) {
    const next = PRESETS.find((p) => p.name === name)
    if (!next) return
    // A name the user did not change follows the preset.
    if (label === preset.name) setLabel(next.name)
    setPreset(next)
    setBaseUrl(next.baseUrl ?? '')
    setModel(next.model)
    setInvalid(false)
  }

  function submit(e: React.FormEvent) {
    e.preventDefault()
    const needsUrl = preset.kind === 'openai'
    if (!label.trim() || !model.trim() || (needsUrl && !baseUrl.trim())) {
      setInvalid(true)
      return
    }
    const input: BackendInput = {
      ...(backend ? { id: backend.id } : {}),
      kind: preset.kind,
      label: label.trim(),
      base_url: needsUrl ? baseUrl.trim() : null,
      model: model.trim(),
    }
    if (!cli) {
      if (key.trim()) input.api_key = key.trim()
      else if (removeKey) input.api_key = ''
    } else if (backend?.has_key) {
      input.api_key = '' // the CLI uses the user's own login: a leftover key from another type goes
    }
    void onSave(input)
  }

  return (
    <form className="ai-form" onSubmit={submit} noValidate>
      <label className="ai-field">
        <span>Type</span>
        <select value={preset.name} onChange={(e) => choose(e.target.value)}>
          {PRESETS.map((p) => (
            <option key={p.name} value={p.name}>
              {p.name}
            </option>
          ))}
        </select>
      </label>
      <label className="ai-field">
        <span>Name</span>
        <input value={label} onChange={(e) => setLabel(e.target.value)} maxLength={200} />
      </label>
      {preset.kind === 'openai' && (
        <label className="ai-field">
          <span>Base URL</span>
          <input
            value={baseUrl}
            inputMode="url"
            autoCapitalize="none"
            spellCheck={false}
            placeholder="https://…/v1"
            onChange={(e) => setBaseUrl(e.target.value)}
          />
        </label>
      )}
      <label className="ai-field">
        <span>Model</span>
        <input value={model} autoCapitalize="none" spellCheck={false} onChange={(e) => setModel(e.target.value)} />
      </label>
      {cli ? (
        <>
          <p className="settings-about">Uses your Claude plan&apos;s limits.</p>
          {backend?.has_key && <p className="settings-about">Saving removes the saved key.</p>}
        </>
      ) : (
        <div className="ai-field">
          <label htmlFor="ai-key">API key</label>
          <input
            id="ai-key"
            type="password"
            autoComplete="off"
            value={key}
            placeholder={hasKey ? 'Saved · type to replace' : ''}
            onChange={(e) => setKey(e.target.value)}
          />
          {hasKey && moved && !key.trim() && <p className="settings-about">Changing the address removes the saved key</p>}
          {hasKey && (
            <button
              className="button-text quiet ai-remove"
              type="button"
              onClick={() => {
                setKey('')
                setRemoveKey(true)
              }}
            >
              Remove key
            </button>
          )}
        </div>
      )}
      {invalid && <p className="form-error">Fill in the name, the model and, for a server, its address.</p>}
      {error && <p className="form-error">{error}</p>}
      <div className="ai-form-actions">
        <button className="button-primary" type="submit" disabled={saving}>
          Save
        </button>
        <button className="button-text neutral" type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  )
}
