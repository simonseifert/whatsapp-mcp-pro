import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { RecordBar } from './RecordBar'

afterEach(cleanup)

const base = { seconds: 0, blob: null, onStop: vi.fn(), onCancel: vi.fn(), onSend: vi.fn() }

describe('RecordBar', () => {
  it('shows the timer as 0:07 while recording', () => {
    render(<RecordBar {...base} state="recording" seconds={7} />)
    expect(screen.getByText('0:07')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Stop recording' }))
    expect(base.onStop).toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Delete recording' }))
    expect(base.onCancel).toHaveBeenCalled()
  })

  it('after stop offers play, delete and send, and sends the blob', () => {
    const blob = new Blob(['x'], { type: 'audio/webm' })
    const onSend = vi.fn()
    render(<RecordBar {...base} state="stopped" seconds={65} blob={blob} onSend={onSend} />)
    expect(screen.getByText('1:05')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Play' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Send voice message' }))
    expect(onSend).toHaveBeenCalledWith(blob)
  })

  it('shows the permission line when denied', () => {
    render(<RecordBar {...base} state="denied" />)
    expect(screen.getByRole('alert').textContent).toBe('Microphone access is off. Allow it in Settings to record.')
  })
})
