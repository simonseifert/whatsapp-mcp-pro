import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { SearchHit } from '../api-types'
import { groupByChat, Search } from './Search'

const ANA = '385910000001@s.whatsapp.net'
const FAMILY = '120363000000000002@g.us'
const hit = (extra: Partial<SearchHit> = {}): SearchHit => ({
  chat_jid: ANA,
  chat_name: 'Ana Kovačević',
  message_id: 'A1',
  ts: '2026-11-20T10:00:00+00:00',
  sender_name: 'Ana Kovačević',
  snippet: 'Šaljem vam [račun] za listopad',
  ...extra,
})

function Where() {
  const l = useLocation()
  return <p data-testid="where">{l.pathname + l.search}</p>
}
function renderSearch(initial = '/search') {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <Where />
      <Routes>
        <Route path="/search" element={<Search />} />
        <Route path="/chat/:jid" element={<p>chat</p>} />
        <Route path="/" element={<p>home</p>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('Search screen', () => {
  let search: ReturnType<typeof vi.spyOn>
  beforeEach(() => {
    search = vi.spyOn(api, 'searchMessages').mockResolvedValue({ hits: [], next: null })
  })
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('groups results by chat and marks the matched word without HTML', async () => {
    search.mockResolvedValue({
      hits: [
        hit(),
        hit({ chat_jid: FAMILY, chat_name: 'Obitelj', message_id: 'F1', sender_name: 'Tata', snippet: 'račun <b>x</b> [<i>y</i>]' }),
        hit({ message_id: 'A2', snippet: 'drugi [račun]' }),
      ],
      next: null,
    })
    const { container } = renderSearch('/search?q=racun')
    await screen.findByRole('region', { name: 'Ana Kovačević' })
    expect(screen.getAllByRole('region').map((r) => r.getAttribute('aria-label'))).toEqual(['Ana Kovačević', 'Obitelj'])
    const ana = screen.getByRole('region', { name: 'Ana Kovačević' })
    expect(ana.querySelectorAll('li').length).toBe(2) // both of Ana's hits sit together
    const marks = Array.from(container.querySelectorAll('mark')).map((m) => m.textContent)
    expect(marks).toEqual(['račun', 'račun', '<i>y</i>'])
    expect(container.querySelector('b, i')).toBeNull() // user text never becomes markup
    expect(search).toHaveBeenCalledWith('racun')
  })

  it('tapping a result opens the chat on that message', async () => {
    search.mockResolvedValue({ hits: [hit()], next: null })
    renderSearch('/search?q=racun')
    const link = await screen.findByRole('link', { name: /listopad/ })
    expect(link.getAttribute('href')).toBe(`/chat/${encodeURIComponent(ANA)}?around=A1`)
    fireEvent.click(link)
    expect(screen.getByTestId('where').textContent).toBe(`/chat/${encodeURIComponent(ANA)}?around=A1`)
  })

  it('searches as you type, keeps the text in the URL and ignores one letter', async () => {
    renderSearch()
    const box = screen.getByRole('searchbox')
    fireEvent.change(box, { target: { value: 'r' } })
    await new Promise((r) => setTimeout(r, 350))
    expect(search).not.toHaveBeenCalled()
    expect(screen.getByText(/at least 2 letters/)).toBeTruthy()
    fireEvent.change(box, { target: { value: 'rac' } })
    await waitFor(() => expect(search).toHaveBeenCalledWith('rac'))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/search?q=rac'))
    expect(await screen.findByText('No messages found.')).toBeTruthy()
  })

  it('shows more results with the cursor of the last page', async () => {
    search.mockResolvedValueOnce({ hits: [hit()], next: '100.5' })
    search.mockResolvedValueOnce({ hits: [hit({ message_id: 'A9', snippet: 'stariji [račun]' })], next: null })
    renderSearch('/search?q=racun')
    fireEvent.click(await screen.findByRole('button', { name: 'Show more results' }))
    await screen.findByText('stariji', { exact: false })
    expect(search).toHaveBeenLastCalledWith('racun', { before: '100.5' })
    expect(screen.queryByRole('button', { name: 'Show more results' })).toBeNull()
  })

  it('says so when the search fails and tries again', async () => {
    search.mockRejectedValueOnce(new Error('boom'))
    search.mockResolvedValueOnce({ hits: [hit()], next: null })
    renderSearch('/search?q=racun')
    fireEvent.click(await screen.findByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('listopad', { exact: false })).toBeTruthy()
  })
})

describe('groupByChat', () => {
  it('keeps the order chats first appear in', () => {
    const groups = groupByChat([hit({ message_id: '1' }), hit({ chat_jid: FAMILY, message_id: '2' }), hit({ message_id: '3' })])
    expect(groups.map((g) => [g.chat_jid, g.hits.map((h) => h.message_id)])).toEqual([
      [ANA, ['1', '3']],
      [FAMILY, ['2']],
    ])
  })
})
