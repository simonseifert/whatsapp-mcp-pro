import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from './api'
import { READ_DEBOUNCE_MS, useMarkRead } from './useMarkRead'

function Probe(props: Parameters<typeof useMarkRead>[0]) {
  useMarkRead(props)
  return null
}
const base = { jid: 'a@s.whatsapp.net', ready: true, newestKey: 'a/1', unread: 2, markedUnread: false, atBottom: true }

describe('useMarkRead', () => {
  let spy: ReturnType<typeof vi.spyOn>
  beforeEach(() => {
    vi.useFakeTimers()
    spy = vi.spyOn(api, 'markRead').mockResolvedValue({ unread: 0, receipts_sent: 0 })
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('marks an opened chat with unread messages once, not again while nothing new arrived', async () => {
    const { rerender } = render(<Probe {...base} />)
    await act(() => vi.advanceTimersByTimeAsync(10))
    expect(spy).toHaveBeenCalledTimes(1)
    rerender(<Probe {...base} unread={0} />)
    await act(() => vi.advanceTimersByTimeAsync(3000))
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('does nothing for a chat opened with nothing unread', async () => {
    render(<Probe {...base} unread={0} />)
    await act(() => vi.advanceTimersByTimeAsync(3000))
    expect(spy).not.toHaveBeenCalled()
  })

  it('waits for the newest message to be on screen', async () => {
    const { rerender } = render(<Probe {...base} atBottom={false} />)
    await act(() => vi.advanceTimersByTimeAsync(3000))
    expect(spy).not.toHaveBeenCalled()
    rerender(<Probe {...base} atBottom />)
    await act(() => vi.advanceTimersByTimeAsync(10))
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('marks a live arrival again, debounced', async () => {
    const { rerender } = render(<Probe {...base} unread={0} />)
    rerender(<Probe {...base} unread={0} newestKey="a/2" />)
    await act(() => vi.advanceTimersByTimeAsync(READ_DEBOUNCE_MS - 100))
    rerender(<Probe {...base} unread={0} newestKey="a/3" />)
    await act(() => vi.advanceTimersByTimeAsync(READ_DEBOUNCE_MS - 100))
    expect(spy).not.toHaveBeenCalled()
    await act(() => vi.advanceTimersByTimeAsync(200))
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('waits for a hidden page to become visible', async () => {
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })
    render(<Probe {...base} />)
    await act(() => vi.advanceTimersByTimeAsync(3000))
    expect(spy).not.toHaveBeenCalled()
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
      await vi.advanceTimersByTimeAsync(10)
    })
    expect(spy).toHaveBeenCalledTimes(1)
  })
})

describe('useMarkRead (review fixes)', () => {
  let spy: ReturnType<typeof vi.spyOn>
  beforeEach(() => {
    vi.useFakeTimers()
    spy = vi.spyOn(api, 'markRead').mockResolvedValue({ unread: 0, receipts_sent: 0 })
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })
  const open = { jid: 'a@s.whatsapp.net', ready: true, newestKey: 'a/1', atBottom: true, markedUnread: false }

  it('waits for the chat detail: messages first, unread 2 afterwards', async () => {
    const { rerender } = render(<Probe {...open} unread={undefined} />)
    await act(() => vi.advanceTimersByTimeAsync(3000))
    expect(spy).not.toHaveBeenCalled()
    rerender(<Probe {...open} unread={2} />)
    await act(() => vi.advanceTimersByTimeAsync(10))
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('marks a chat with the phone dot even with nothing unread', async () => {
    render(<Probe {...open} unread={0} markedUnread />)
    await act(() => vi.advanceTimersByTimeAsync(10))
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('rechecks visibility when the debounce timer fires', async () => {
    const { rerender } = render(<Probe {...open} unread={0} />)
    rerender(<Probe {...open} unread={0} newestKey="a/2" />)
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })
    await act(() => vi.advanceTimersByTimeAsync(READ_DEBOUNCE_MS + 100))
    expect(spy).not.toHaveBeenCalled()
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
  })
})
