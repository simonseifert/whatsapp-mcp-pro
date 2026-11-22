import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { VoiceNote } from './VoiceNote'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

describe('VoiceNote', () => {
  it('shows the duration and cycles the playback speed', () => {
    render(<VoiceNote chat="a@s.whatsapp.net" id="M1" durationS={37} />)
    expect(screen.getByText('0:37')).toBeTruthy()
    const speed = screen.getByRole('button', { name: 'Playback speed 1×' })
    fireEvent.click(speed)
    expect(screen.getByRole('button', { name: 'Playback speed 1.5×' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Playback speed 1.5×' }))
    fireEvent.click(screen.getByRole('button', { name: 'Playback speed 2×' }))
    expect(screen.getByRole('button', { name: 'Playback speed 1×' })).toBeTruthy()
  })

  it('fetches the text once, then only toggles it', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ text: 'Kupi kruh.', language: 'hr' }))
    vi.stubGlobal('fetch', fetchMock)
    render(<VoiceNote chat="a@s.whatsapp.net" id="M1" durationS={5} />)
    fireEvent.click(screen.getByRole('button', { name: 'Show text' }))
    expect(await screen.findByText('Kupi kruh.')).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/transcripts/a%40s.whatsapp.net/M1')
    expect(init.method).toBe('POST')
    fireEvent.click(screen.getByRole('button', { name: 'Hide text' }))
    expect(screen.queryByText('Kupi kruh.')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Show text' }))
    expect(screen.getByText('Kupi kruh.')).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('shows the server error and lets the user try again', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json({ error: 'No transcription backend' }, 503))
      .mockResolvedValueOnce(json({ text: 'Ok.', language: 'en' }))
    vi.stubGlobal('fetch', fetchMock)
    render(<VoiceNote chat="a@s.whatsapp.net" id="M1" durationS={5} />)
    fireEvent.click(screen.getByRole('button', { name: 'Show text' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe('No transcription backend'))
    fireEvent.click(screen.getByRole('button', { name: 'Show text' }))
    expect(await screen.findByText('Ok.')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('has a keyboard-operable seek slider', () => {
    render(<VoiceNote chat="a@s.whatsapp.net" id="M1" durationS={37} />)
    const slider = screen.getByRole('slider', { name: 'Seek voice message' })
    expect(slider.getAttribute('aria-valuemin')).toBe('0')
    expect(slider.getAttribute('aria-valuemax')).toBe('37')
    expect(slider.getAttribute('aria-valuenow')).toBe('0')
    const audio = document.querySelector('audio') as HTMLAudioElement
    fireEvent.keyDown(slider, { key: 'ArrowRight' })
    expect(audio.currentTime).toBe(5)
    expect(slider.getAttribute('aria-valuenow')).toBe('5')
    fireEvent.keyDown(slider, { key: 'End' })
    expect(slider.getAttribute('aria-valuenow')).toBe('37')
  })
})
