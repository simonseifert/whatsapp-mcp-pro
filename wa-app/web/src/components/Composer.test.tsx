import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Composer, MIC_EXPLAINER } from './Composer'

class FakeRecorder {
  static isTypeSupported = (t: string) => t === 'audio/mp4'
  state = 'inactive'
  mimeType = 'audio/mp4'
  l: Record<string, (e: { data?: Blob }) => void> = {}
  start() {
    this.state = 'recording'
  }
  stop() {
    this.state = 'inactive'
    this.l.dataavailable?.({ data: new Blob(['abc'], { type: 'audio/mp4' }) })
    this.l.stop?.({})
  }
  addEventListener(n: string, f: (e: { data?: Blob }) => void) {
    this.l[n] = f
  }
}

beforeEach(() => {
  localStorage.setItem('wa-mic-explained', '1')
  vi.stubGlobal('MediaRecorder', FakeRecorder)
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: vi.fn() }] }) },
    configurable: true,
  })
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('Composer voice notes', () => {
  it('shows a mic with an empty box and a send button once there is text', () => {
    render(<Composer replyTo={null} onCancelReply={() => {}} onSend={() => {}} onVoice={() => {}} />)
    expect(screen.getByRole('button', { name: 'Record voice message' })).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Bok' } })
    expect(screen.queryByRole('button', { name: 'Record voice message' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Send' })).toBeTruthy()
  })

  it('records, stops and sends an m4a file only after Send', async () => {
    const onVoice = vi.fn()
    render(<Composer replyTo={null} onCancelReply={() => {}} onSend={() => {}} onVoice={onVoice} />)
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Record voice message' })))
    fireEvent.click(screen.getByRole('button', { name: 'Stop recording' }))
    expect(onVoice).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Send voice message' }))
    expect(onVoice).toHaveBeenCalledTimes(1)
    expect(onVoice.mock.calls[0][0].name).toBe('voice-note.m4a')
    expect(screen.getByLabelText('Message')).toBeTruthy()
  })

  it('explains the microphone once before the first request, then asks straight away', async () => {
    localStorage.removeItem('wa-mic-explained')
    render(<Composer replyTo={null} onCancelReply={() => {}} onSend={() => {}} onVoice={() => {}} />)
    const getUserMedia = navigator.mediaDevices.getUserMedia as ReturnType<typeof vi.fn>
    fireEvent.click(screen.getByRole('button', { name: 'Record voice message' }))
    expect(screen.getByText(MIC_EXPLAINER)).toBeTruthy()
    expect(getUserMedia).not.toHaveBeenCalled()
    expect(localStorage.getItem('wa-mic-explained')).toBe('1')
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Continue' })))
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(screen.queryByText(MIC_EXPLAINER)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Delete recording' }))
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Record voice message' })))
    expect(screen.queryByText(MIC_EXPLAINER)).toBeNull()
    expect(getUserMedia).toHaveBeenCalledTimes(2)
  })
})
