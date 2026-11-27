import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MAX_SECONDS, useRecorder } from './useRecorder'

type Listener = (e: { data?: Blob }) => void

class FakeRecorder {
  static supported: string[] = []
  static last: FakeRecorder | null = null
  static isTypeSupported = (t: string) => FakeRecorder.supported.includes(t)
  state: 'inactive' | 'recording' = 'inactive'
  mimeType: string
  listeners: Record<string, Listener> = {}
  constructor(_stream: MediaStream, opts: { mimeType: string }) {
    this.mimeType = opts.mimeType
    FakeRecorder.last = this
  }
  start() {
    this.state = 'recording'
  }
  stop() {
    this.state = 'inactive'
    this.listeners.dataavailable?.({ data: new Blob(['abc'], { type: this.mimeType }) })
    this.listeners.stop?.({})
  }
  addEventListener(name: string, fn: Listener) {
    this.listeners[name] = fn
  }
}

const trackStop = vi.fn()
const getUserMedia = vi.fn()

beforeEach(() => {
  vi.useFakeTimers()
  trackStop.mockReset()
  FakeRecorder.supported = ['audio/webm;codecs=opus', 'audio/mp4']
  getUserMedia.mockReset().mockResolvedValue({ getTracks: () => [{ stop: trackStop }] })
  vi.stubGlobal('MediaRecorder', FakeRecorder)
  Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia }, configurable: true })
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('useRecorder', () => {
  it('prefers webm opus when supported', async () => {
    const { result } = renderHook(() => useRecorder())
    await act(() => result.current.start())
    expect(FakeRecorder.last?.mimeType).toBe('audio/webm;codecs=opus')
    expect(result.current.state).toBe('recording')
  })

  it('picks audio/mp4 when webm is unsupported', async () => {
    FakeRecorder.supported = ['audio/mp4']
    const { result } = renderHook(() => useRecorder())
    await act(() => result.current.start())
    expect(FakeRecorder.last?.mimeType).toBe('audio/mp4')
  })

  it('counts seconds, stops with a blob and releases the microphone', async () => {
    const { result } = renderHook(() => useRecorder())
    await act(() => result.current.start())
    act(() => void vi.advanceTimersByTime(7000))
    expect(result.current.seconds).toBe(7)
    act(() => result.current.stop())
    expect(result.current.state).toBe('stopped')
    expect(result.current.blob?.size).toBe(3)
    expect(result.current.seconds).toBe(7)
    expect(trackStop).toHaveBeenCalled()
  })

  it('cancel discards the blob and releases the microphone', async () => {
    const { result } = renderHook(() => useRecorder())
    await act(() => result.current.start())
    act(() => result.current.stop())
    expect(result.current.blob).not.toBeNull()
    act(() => result.current.cancel())
    expect(result.current.blob).toBeNull()
    expect(result.current.state).toBe('idle')
    expect(result.current.seconds).toBe(0)
  })

  it('cancel while recording sends nothing and frees the microphone', async () => {
    const { result } = renderHook(() => useRecorder())
    await act(() => result.current.start())
    act(() => result.current.cancel())
    expect(result.current.blob).toBeNull()
    expect(result.current.state).toBe('idle')
    expect(trackStop).toHaveBeenCalled()
  })

  it('reports denied when permission is refused', async () => {
    getUserMedia.mockRejectedValue(new DOMException('no', 'NotAllowedError'))
    const { result } = renderHook(() => useRecorder())
    await act(() => result.current.start())
    expect(result.current.state).toBe('denied')
  })

  it('treats a hidden page as stop and keeps the blob', async () => {
    const { result } = renderHook(() => useRecorder())
    await act(() => result.current.start())
    Object.defineProperty(document, 'hidden', { value: true, configurable: true })
    act(() => void document.dispatchEvent(new Event('visibilitychange')))
    Object.defineProperty(document, 'hidden', { value: false, configurable: true })
    expect(result.current.state).toBe('stopped')
    expect(result.current.blob).not.toBeNull()
    expect(trackStop).toHaveBeenCalled()
  })

  it('stops by itself at 15 minutes', async () => {
    const { result } = renderHook(() => useRecorder())
    await act(() => result.current.start())
    act(() => void vi.advanceTimersByTime(MAX_SECONDS * 1000))
    expect(result.current.state).toBe('stopped')
    expect(result.current.seconds).toBe(MAX_SECONDS)
  })

  it('releases the microphone on unmount', async () => {
    const { result, unmount } = renderHook(() => useRecorder())
    await act(() => result.current.start())
    unmount()
    expect(trackStop).toHaveBeenCalled()
  })
})
