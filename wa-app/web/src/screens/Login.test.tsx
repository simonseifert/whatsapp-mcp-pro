import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Login } from './Login'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('Login', () => {
  it('asks for the passphrase twice on first run and blocks a mismatch', () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    render(<Login setupNeeded onDone={() => {}} />)
    fireEvent.change(screen.getByLabelText('Passphrase'), { target: { value: 'long enough one' } })
    fireEvent.change(screen.getByLabelText('Repeat passphrase'), { target: { value: 'something else!' } })
    fireEvent.click(screen.getByRole('button', { name: 'Set passphrase' }))
    expect(screen.getByRole('alert').textContent).toBe('The passphrases do not match')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('shows one field and the server error on a wrong passphrase', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: 'Wrong passphrase' }), { status: 401 })),
    )
    window.history.pushState({}, '', '/login')
    render(<Login setupNeeded={false} onDone={() => {}} />)
    expect(screen.queryByLabelText('Repeat passphrase')).toBeNull()
    fireEvent.change(screen.getByLabelText('Passphrase'), { target: { value: 'wrong wrong' } })
    fireEvent.click(screen.getByRole('button', { name: 'Log in' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe('Wrong passphrase'))
  })
})
