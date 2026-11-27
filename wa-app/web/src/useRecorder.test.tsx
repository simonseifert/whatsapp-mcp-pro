import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MAX_SECONDS, useRecorder } from './useRecorder'

type Listener = (e: { data?: Blob }) => void

class FakeRecorder {
  static supported: string[] = []
  static last: FakeRecorder | null = null
  static async = false
  static throwOnStart = false
  static isTypeSupported = (t: string) => FakeRecorder.supported.includes(t)
  state: 'inactive' | 'recording' = 'inactive'
  mimeType: string
  listeners: Record<string, Listener> = {}
  constructor(_stream: MediaStream, opts: { mimeType: string }) {
    this.mimeType = opts.mimeType
    FakeRecorder.last = this
  }
  start() {
    if (FakeRecorder.throwOnStart) throw new Error('boom')
    this.state = 'recording'
  }
  stop() {
    this.state = 'inactive'
    const fire = () => {
      this.listeners.dataavailable?.({ data: new Blob(['abc'], { type: this.mimeType }) })
      this.listeners.stop?.({})
    }
    if (FakeRecorder.async) queueMicrotask(fire)
    else fire()
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
  FakeRecorder.async = false
  FakeRecorder.throwOnStart = false
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

  it('cancel while asking starts no recording and stops the stream when it arrives', async () => {
    let resolve: (s: unknown) => void = () => {}
    getUserMedia.mockReturnValue(new Promise((r) => (resolve = r)))
    FakeRecorder.last = null
    const { result } = renderHook(() => useRecorder())
    let started: Promise<void> = Promise.resolve()
    act(() => {
      started = result.current.start()
    })
    expect(result.current.state).toBe('asking')
    act(() => result.current.cancel())
    await act(async () => {
      resolve({ getTracks: () => [{ stop: trackStop }] })
      await started
    })
    expect(FakeRecorder.last).toBeNull()
    expect(result.current.state).toBe('idle')
    expect(trackStop).toHaveBeenCalled()
  })

  it('unmount while asking stops the stream when it arrives', async () => {
    let resolve: (s: unknown) => void = () => {}
    getUserMedia.mockReturnValue(new Promise((r) => (resolve = r)))
    FakeRecorder.last = null
    const { result, unmount } = renderHook(() => useRecorder())
    let started: Promise<void> = Promise.resolve()
    act(() => {
      started = result.current.start()
    })
    unmount()
    await act(async () => {
      resolve({ getTracks: () => [{ stop: trackStop }] })
      await started
    })
    expect(FakeRecorder.last).toBeNull()
    expect(trackStop).toHaveBeenCalled()
  })

  it('a late stop event from a cancelled recorder does not touch the next recording', async () => {
    FakeRecorder.async = true
    const { result } = renderHook(() => useRecorder())
    await act(() => result.current.start())
    const first = FakeRecorder.last
    act(() => result.current.cancel())
    await act(() => result.current.start())
    expect(FakeRecorder.last).not.toBe(first)
    trackStop.mockClear()
    await act(async () => {
      await Promise.resolve() // the old recorder's stop event arrives now
    })
    expect(result.current.state).toBe('recording')
    expect(result.current.blob).toBeNull()
    expect(trackStop).not.toHaveBeenCalled()
  })

  it('releases the microphone at once when hidden, before the stop event', async () => {
    FakeRecorder.async = true
    const { result } = renderHook(() => useRecorder())
    await act(() => result.current.start())
    Object.defineProperty(document, 'hidden', { value: true, configurable: true })
    act(() => void document.dispatchEvent(new Event('visibilitychange')))
    Object.defineProperty(document, 'hidden', { value: false, configurable: true })
    expect(trackStop).toHaveBeenCalled()
    await act(async () => {
      await Promise.resolve()
    })
    expect(result.current.state).toBe('stopped')
  })

  it('maps microphone errors by name', async () => {
    const { result } = renderHook(() => useRecorder())
    getUserMedia.mockRejectedValue(new DOMException('x', 'NotFoundError'))
    await act(() => result.current.start())
    expect(result.current.message).toBe('No microphone found.')
    getUserMedia.mockRejectedValue(new DOMException('x', 'SecurityError'))
    await act(() => result.current.start())
    expect(result.current.message).toBe('Microphone access is off. Allow it in Settings to record.')
    getUserMedia.mockRejectedValue(new DOMException('x', 'AbortError'))
    await act(() => result.current.start())
    expect(result.current.message).toBe("Couldn't start the microphone.")
  })

  it('a recorder that throws on start frees the microphone and goes back to idle', async () => {
    FakeRecorder.throwOnStart = true
    const { result } = renderHook(() => useRecorder())
    await act(() => result.current.start())
    expect(result.current.state).toBe('idle')
    expect(result.current.message).toBe("Couldn't start the microphone.")
    expect(trackStop).toHaveBeenCalled()
  })
})
