import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, apiFetch, navigation } from './api'

function mockFetch(status: number, body: unknown) {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status })))
}

beforeEach(() => {
  vi.spyOn(navigation, 'toLogin').mockImplementation(() => {})
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('apiFetch', () => {
  it('returns parsed JSON on success', async () => {
    mockFetch(200, { ok: true })
    await expect(apiFetch('/api/health')).resolves.toEqual({ ok: true })
  })

  it('throws ApiError with status and server message', async () => {
    mockFetch(401, { error: 'Wrong passphrase' })
    await expect(apiFetch('/api/auth/login', { method: 'POST', body: '{}' })).rejects.toMatchObject({
      status: 401,
      message: 'Wrong passphrase',
    })
    await expect(apiFetch('/api/auth/login')).rejects.toBeInstanceOf(ApiError)
  })

  it('redirects to /login on 401 when elsewhere', async () => {
    window.history.pushState({}, '', '/')
    const toLogin = vi.mocked(navigation.toLogin)
    mockFetch(401, { error: 'Not logged in' })
    await expect(apiFetch('/api/chats')).rejects.toBeInstanceOf(ApiError)
    expect(toLogin).toHaveBeenCalledOnce()
  })

  it('does not redirect when already on /login', async () => {
    window.history.pushState({}, '', '/login')
    const toLogin = vi.mocked(navigation.toLogin)
    mockFetch(401, { error: 'Wrong passphrase' })
    await expect(apiFetch('/api/auth/login')).rejects.toBeInstanceOf(ApiError)
    expect(toLogin).not.toHaveBeenCalled()
  })
})
