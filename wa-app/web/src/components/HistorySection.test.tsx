import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { HistoryCoverage, HistoryStatus } from '../api-types'
import { HistorySection } from './HistorySection'

const chat = (jid: string, name: string, messages: number, own: number, thin: boolean, oldest = '2019-03-15T10:00:00Z') => ({
  jid,
  name,
  kind: 'direct',
  messages,
  own_texts: own,
  oldest,
  last_active: '2026-11-01T10:00:00Z',
  thin,
  backfill_jid: null,
})
const coverage: HistoryCoverage = {
  totals: { messages: 12345, chats: 87, oldest: '2019-03-15T10:00:00Z' },
  thin_threshold: 30,
  chats: [chat('ana@x', 'Ana', 400, 120, false), chat('bo@x', 'Bo', 12, 3, true, '2025-06-10T10:00:00Z'), chat('cy@x', 'Cy', 0, 0, true)],
}
const idle: HistoryStatus = { running: null, queued: [], done: [] }
const running: HistoryStatus = { running: { jid: 'bo@x', batches: 2, oldest: '2025-01-10T10:00:00Z' }, queued: ['cy@x', 'zz@x'], done: [] }

async function flush() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
}
async function tick(ms = 5000) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}
async function open() {
  render(<HistorySection />)
  await flush()
}

let hidden = false
beforeEach(() => {
  vi.useFakeTimers()
  hidden = false
  vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden)
  vi.spyOn(api, 'fetchCoverage').mockResolvedValue(coverage)
  vi.spyOn(api, 'fetchHistoryStatus').mockResolvedValue(idle)
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('HistorySection', () => {
  it('shows totals and the thin count', async () => {
    await open()
    expect(screen.getByText('12,345 messages in 87 chats, oldest Mar 2019')).toBeTruthy()
    expect(screen.getByText('2 of your 3 most active chats have too little of your writing for good drafts')).toBeTruthy()
  })

  it('lists each chat with counts, oldest month and a Thin tag on thin ones', async () => {
    await open()
    const bo = screen.getByText('Bo').closest('li') as HTMLElement
    expect(bo.textContent).toContain('12 messages')
    expect(bo.textContent).toContain('3 from you')
    expect(bo.textContent).toContain('oldest Jun 2025')
    expect(bo.textContent).toContain('Thin')
    const ana = screen.getByText('Ana').closest('li') as HTMLElement
    expect(ana.textContent).toContain('400 messages')
    expect(ana.textContent).not.toContain('Thin')
  })

  it('confirms before filling, then queues the thin chats for the chosen depth', async () => {
    const fill = vi.spyOn(api, 'fillHistory').mockResolvedValue({ added: 2, status: { ...idle, queued: ['bo@x'] } })
    await open()
    expect((screen.getByRole('radio', { name: '3 years' }) as HTMLInputElement).checked).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Fill thin chats' }))
    expect(screen.getByText("Your phone will show a 'Finished syncing' notice for each batch. Keep it online and charging.")).toBeTruthy()
    expect(fill).not.toHaveBeenCalled()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    })
    expect(fill).toHaveBeenCalledWith('thin', '3y')
    expect(screen.queryByRole('button', { name: 'Continue' })).toBeNull()
  })

  it('Cancel on the confirmation fills nothing; depth and Fill all are passed through', async () => {
    const fill = vi.spyOn(api, 'fillHistory').mockResolvedValue({ added: 3, status: idle })
    await open()
    fireEvent.click(screen.getByRole('button', { name: 'Fill thin chats' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(fill).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('radio', { name: 'Everything' }))
    fireEvent.click(screen.getByRole('button', { name: 'Fill all 3' }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    })
    expect(fill).toHaveBeenCalledWith('all', 'all')
  })

  it('says so when a fill adds nothing', async () => {
    vi.spyOn(api, 'fillHistory').mockResolvedValue({ added: 0, status: idle })
    await open()
    fireEvent.click(screen.getByRole('button', { name: 'Fill thin chats' }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    })
    expect(screen.getByText('Nothing to fill. Every chat is already queued or has enough history.')).toBeTruthy()
  })

  it('shows a paused skip with its own reason', async () => {
    vi.spyOn(api, 'fetchHistoryStatus').mockResolvedValue({
      running: null,
      queued: [],
      done: [{ jid: 'cy@x', result: 'skipped', detail: 'Paused: phone not answering', oldest: null }],
    })
    await open()
    expect(screen.getByText('Cy: Paused: phone not answering')).toBeTruthy()
  })

  it('shows a failed fill', async () => {
    vi.spyOn(api, 'fillHistory').mockRejectedValue(new api.ApiError(503, 'Bridge is down'))
    await open()
    fireEvent.click(screen.getByRole('button', { name: 'Fill thin chats' }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    })
    expect(screen.getByRole('alert').textContent).toBe('Bridge is down')
  })

  it('shows progress while running, polls every 5 s and refreshes coverage with results when done', async () => {
    const status = vi.spyOn(api, 'fetchHistoryStatus').mockResolvedValue(running)
    await open()
    expect(screen.getByText('Filling Bo: 2 batches, back to Jan 2025')).toBeTruthy()
    expect(screen.getByText('2 waiting')).toBeTruthy()
    expect(status).toHaveBeenCalledTimes(1)
    await tick(4900)
    expect(status).toHaveBeenCalledTimes(1)
    await tick(100)
    expect(status).toHaveBeenCalledTimes(2)
    expect(api.fetchCoverage).toHaveBeenCalledTimes(1)

    status.mockResolvedValue({
      running: null,
      queued: [],
      done: [
        { jid: 'bo@x', result: 'done', detail: '', oldest: null },
        { jid: 'cy@x', result: 'skipped', detail: 'no messages', oldest: null },
        { jid: 'ana@x', result: 'more', detail: '', oldest: null },
        { jid: 'zz@x', result: 'failed', detail: 'phone offline', oldest: null },
        { jid: 'ana@x', result: 'failed', detail: '', oldest: null },
      ],
    })
    await tick()
    expect(screen.queryByText(/^Filling/)).toBeNull()
    expect(screen.getByText('Bo: Done')).toBeTruthy()
    expect(screen.getByText('Cy: Skipped: nothing to anchor on')).toBeTruthy()
    expect(screen.getByText('Ana: More available, run again')).toBeTruthy()
    expect(screen.getByText('zz: Failed: phone offline')).toBeTruthy()
    expect(screen.getByText('Ana: Failed')).toBeTruthy()
    expect(screen.getByText('Last fill')).toBeTruthy()
    expect(api.fetchCoverage).toHaveBeenCalledTimes(2)
    // idle now: no more polling
    await tick(20000)
    expect(status).toHaveBeenCalledTimes(3)
  })

  it('does not poll while the page is hidden, and catches up when it is shown again', async () => {
    const status = vi.spyOn(api, 'fetchHistoryStatus').mockResolvedValue(running)
    await open()
    hidden = true
    await tick(15000)
    expect(status).toHaveBeenCalledTimes(1)
    hidden = false
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    expect(status).toHaveBeenCalledTimes(2)
  })

  it('stops polling on unmount', async () => {
    const status = vi.spyOn(api, 'fetchHistoryStatus').mockResolvedValue(running)
    const { unmount } = render(<HistorySection />)
    await flush()
    unmount()
    await tick(20000)
    expect(status).toHaveBeenCalledTimes(1)
  })

  it('Stop cancels what is queued', async () => {
    vi.spyOn(api, 'fetchHistoryStatus').mockResolvedValue(running)
    const cancel = vi.spyOn(api, 'cancelHistory').mockResolvedValue({ ...running, queued: [] })
    await open()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    })
    expect(cancel).toHaveBeenCalled()
    expect(screen.queryByText('2 waiting')).toBeNull()
  })

  it('says so when coverage cannot load', async () => {
    vi.spyOn(api, 'fetchCoverage').mockRejectedValue(new Error('x'))
    await open()
    expect(screen.getByText("Couldn't load the history coverage.")).toBeTruthy()
  })
})
