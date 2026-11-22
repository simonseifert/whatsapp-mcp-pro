export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export const navigation = { toLogin: () => window.location.assign('/login') }

/** JSON fetch against the app's own API. Throws ApiError; a 401 outside /login sends the user to /login. */
export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (init?.body !== undefined && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const response = await fetch(path, { ...init, headers, credentials: 'same-origin' })
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    if (response.status === 401 && window.location.pathname !== '/login') navigation.toLogin()
    const message = body && typeof body === 'object' && 'error' in body ? String(body.error) : response.statusText
    throw new ApiError(response.status, message)
  }
  return body as T
}
