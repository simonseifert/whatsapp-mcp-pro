import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Media } from './Media'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const base = { filename: null, duration_s: null, size_bytes: null }

describe('Media', () => {
  it('requests nothing for media with no file and shows a placeholder', () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    render(<Media chat="a@s.whatsapp.net" id="M1" media={{ ...base, type: 'image', has_file: false }} />)
    expect(screen.getByText('Media not available')).toBeTruthy()
    expect(document.querySelector('img')).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('only the Try again tap uses the retry URL, and a rate limit is shown', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{}', { status: 429 }))
    vi.stubGlobal('fetch', fetchMock)
    render(<Media chat="a@s.whatsapp.net" id="M1" media={{ ...base, type: 'image', has_file: false }} />)
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe('Asked recently, try again later'))
    expect(fetchMock.mock.calls[0][0]).toBe('/api/media/a%40s.whatsapp.net/M1/retry')
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: 'POST' })
  })

  it('shows the picture once a retry finds the file, and opens a viewer on tap', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('x', { status: 200 })))
    render(<Media chat="a@s.whatsapp.net" id="M1" media={{ ...base, type: 'image', has_file: false }} />)
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    const open = await screen.findByRole('button', { name: 'Open photo' })
    expect(open.querySelector('img')?.getAttribute('src')).toBe('/api/media/a%40s.whatsapp.net/M1/thumb')
    fireEvent.click(open)
    expect(screen.getByRole('dialog').querySelector('img')?.getAttribute('src')).toBe('/api/media/a%40s.whatsapp.net/M1')
    fireEvent.click(screen.getByRole('button', { name: 'Close photo' }))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('renders a document as a file row with its size', () => {
    render(
      <Media
        chat="a@s.whatsapp.net"
        id="D1"
        media={{ type: 'document', filename: 'plan.pdf', duration_s: null, has_file: true, size_bytes: 734_003 }}
      />,
    )
    const link = screen.getByRole('link')
    expect(link.textContent).toContain('plan.pdf')
    expect(link.textContent).toContain('717 KB')
  })

  it('falls back to the placeholder when the audio cannot load', () => {
    render(
      <Media
        chat="a@s.whatsapp.net"
        id="V1"
        media={{ type: 'ptt', filename: null, duration_s: 9, has_file: true, size_bytes: null }}
      />,
    )
    fireEvent.error(document.querySelector('audio') as HTMLAudioElement)
    expect(screen.getByText(/Media not available/)).toBeTruthy()
  })
})
