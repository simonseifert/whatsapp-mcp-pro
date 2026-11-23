import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { BackendCfg } from '../api-types'
import { BackendForm } from './BackendForm'

afterEach(cleanup)

const saved: BackendCfg = {
  id: 7,
  kind: 'anthropic',
  label: 'Work key',
  base_url: null,
  model: 'claude-sonnet-5-5',
  has_key: true,
}

const field = (name: string) => screen.getByLabelText(name) as HTMLInputElement

describe('BackendForm presets', () => {
  it('fills the fields for each preset', () => {
    render(<BackendForm onSave={vi.fn()} onCancel={() => {}} />)
    const preset = screen.getByLabelText('Type') as HTMLSelectElement
    expect(field('Model').value).toBe('sonnet') // first preset: Claude (claude CLI)
    expect(screen.queryByLabelText('API key')).toBeNull()
    expect(screen.getByText("Uses your Claude plan's limits.")).toBeTruthy()

    fireEvent.change(preset, { target: { value: 'Local (Ollama)' } })
    expect(field('Base URL').value).toBe('http://127.0.0.1:11434/v1')
    expect(field('Model').value).toBe('')
    expect(screen.getByLabelText('API key')).toBeTruthy()

    fireEvent.change(preset, { target: { value: 'Anthropic API' } })
    expect(field('Model').value).toBe('claude-sonnet-5-5')
    expect(screen.queryByLabelText('Base URL')).toBeNull()

    fireEvent.change(preset, { target: { value: 'OpenAI-compatible' } })
    expect(field('Base URL').value).toBe('')
    expect(field('Model').value).toBe('')
  })

  it('saves the chosen preset as the right kind', () => {
    const onSave = vi.fn()
    render(<BackendForm onSave={onSave} onCancel={() => {}} />)
    fireEvent.change(screen.getByLabelText('Type'), { target: { value: 'Local (Ollama)' } })
    fireEvent.change(field('Model'), { target: { value: 'llama3' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(onSave).toHaveBeenCalledWith({
      kind: 'openai',
      label: 'Local (Ollama)',
      base_url: 'http://127.0.0.1:11434/v1',
      model: 'llama3',
    })
  })
})

describe('BackendForm key', () => {
  it('never prefills the key and omits it when untouched', () => {
    const onSave = vi.fn()
    render(<BackendForm backend={saved} onSave={onSave} onCancel={() => {}} />)
    const key = field('API key')
    expect(key.value).toBe('')
    expect(key.placeholder).toBe('Saved · type to replace')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(onSave.mock.calls[0]![0]).toEqual({
      id: 7,
      kind: 'anthropic',
      label: 'Work key',
      base_url: null,
      model: 'claude-sonnet-5-5',
    })
  })

  it('sends a typed key, and "" only after Remove key', () => {
    const onSave = vi.fn()
    render(<BackendForm backend={saved} onSave={onSave} onCancel={() => {}} />)
    fireEvent.change(field('API key'), { target: { value: 'sk-test-FICTIONAL' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(onSave.mock.calls[0]![0].api_key).toBe('sk-test-FICTIONAL')

    fireEvent.click(screen.getByRole('button', { name: 'Remove key' }))
    expect(field('API key').placeholder).not.toBe('Saved · type to replace')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(onSave.mock.calls[1]![0].api_key).toBe('')
  })

  it('has no Remove key when nothing is saved', () => {
    render(<BackendForm backend={{ ...saved, has_key: false }} onSave={vi.fn()} onCancel={() => {}} />)
    expect(screen.queryByRole('button', { name: 'Remove key' })).toBeNull()
    expect(field('API key').placeholder).toBe('')
  })

  it('does not save without a model', () => {
    const onSave = vi.fn()
    render(<BackendForm onSave={onSave} onCancel={() => {}} />)
    fireEvent.change(screen.getByLabelText('Type'), { target: { value: 'OpenAI-compatible' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(onSave).not.toHaveBeenCalled()
  })

  it('opens an openai backend with no URL as OpenAI-compatible, not the CLI preset', () => {
    render(<BackendForm backend={{ ...saved, kind: 'openai', base_url: null, has_key: false }} onSave={vi.fn()} onCancel={() => {}} />)
    expect((screen.getByLabelText('Type') as HTMLSelectElement).value).toBe('OpenAI-compatible')
  })

  it('switching a keyed backend to the Claude CLI says Save removes the key, and sends api_key ""', () => {
    const onSave = vi.fn()
    render(<BackendForm backend={saved} onSave={onSave} onCancel={() => {}} />)
    expect(screen.queryByText('Saving removes the saved key.')).toBeNull()
    fireEvent.change(screen.getByLabelText('Type'), { target: { value: 'Claude (claude CLI)' } })
    expect(screen.getByText('Saving removes the saved key.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(onSave.mock.calls[0]![0]).toMatchObject({ id: 7, kind: 'claude_cli', model: 'sonnet', api_key: '' })
  })
})
