import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { LiveEvent } from './api-types'
import { backoffDelay, connectLive, type SourceFactory } from './live'

class FakeSource {
  static all: FakeSource[] = []
  listeners = new Map<string, (e: MessageEvent<string>) => void>()
  onerror: ((e: Event) => void) | null = null
  closed = false
  url: string
  constructor(url: string) {
    this.url = url
    FakeSource.all.push(this)
  }
  addEventListener(type: string, listener: (e: MessageEvent<string>) => void) {
    this.listeners.set(type, listener)
  }
  close() {
    this.closed = true
  }
  emit(type: string, data: unknown, lastEventId = '') {
    this.listeners.get(type)?.({ data: JSON.stringify(data), lastEventId } as MessageEvent<string>)
  }
  fail() {
    this.onerror?.(new Event('error'))
  }
}
const factory: SourceFactory = (url) => new FakeSource(url)

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true })
  document.dispatchEvent(new Event('visibilitychange'))
}

beforeEach(() => {
  vi.useFakeTimers()
  FakeSource.all = []
  setVisibility('visible')
})
afterEach(() => vi.useRealTimers())

describe('backoffDelay', () => {
  it('runs 1 s, 2 s, 4 s ... capped at 30 s', () => {
    expect([0, 1, 2, 3, 4, 5, 6, 20].map(backoffDelay)).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000])
  })
})

describe('connectLive', () => {
  it('reconnects with growing delays, resumes from the last event id and resets after a hello', () => {
    const stop = connectLive(() => {}, { factory, report: () => {} })
    expect(FakeSource.all).toHaveLength(1)
    FakeSource.all[0].emit('chat', {}, '41')
    FakeSource.all[0].fail()
    vi.advanceTimersByTime(999)
    expect(FakeSource.all).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(FakeSource.all).toHaveLength(2)
    expect(FakeSource.all[1].url).toBe('/api/live?last_event_id=41')
    FakeSource.all[1].fail()
    vi.advanceTimersByTime(1999)
    expect(FakeSource.all).toHaveLength(2)
    vi.advanceTimersByTime(1)
    expect(FakeSource.all).toHaveLength(3)
    FakeSource.all[2].emit('hello', { client: 'c1' })
    FakeSource.all[2].fail()
    vi.advanceTimersByTime(1000) // back to 1 s after a successful connection
    expect(FakeSource.all).toHaveLength(4)
    stop()
    FakeSource.all[3].fail()
    vi.advanceTimersByTime(60000)
    expect(FakeSource.all).toHaveLength(4)
  })

  it('passes events through', () => {
    const seen: LiveEvent[] = []
    const stop = connectLive((e) => seen.push(e), { factory, report: () => {} })
    FakeSource.all[0].emit('reaction', { chat_jid: 'a', message_id: 'm' })
    expect(seen).toEqual([{ name: 'reaction', data: { chat_jid: 'a', message_id: 'm' } }])
    stop()
  })

  it('on return to the foreground reconnects and asks for a refetch; reports visibility', () => {
    const seen: LiveEvent[] = []
    const report = vi.fn()
    const stop = connectLive((e) => seen.push(e), { factory, report })
    FakeSource.all[0].emit('hello', { client: 'c1' })
    expect(report).toHaveBeenLastCalledWith('c1', true)
    setVisibility('hidden')
    expect(report).toHaveBeenLastCalledWith('c1', false)
    expect(FakeSource.all).toHaveLength(1)
    setVisibility('visible')
    expect(report).toHaveBeenLastCalledWith('c1', true)
    expect(FakeSource.all).toHaveLength(2)
    expect(FakeSource.all[0].closed).toBe(true)
    expect(seen).toEqual([{ name: 'resync', data: {} }])
    stop()
  })
})
