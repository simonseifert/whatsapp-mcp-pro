import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MessageView } from '../api-types'
import { Bubble } from './Bubble'

const message: MessageView = {
  id: 'M1',
  chat_jid: 'a@s.whatsapp.net',
  sender_jid: 'a@s.whatsapp.net',
  sender_name: 'Ana',
  from_me: false,
  timestamp_iso: '2026-11-22T10:00:00+00:00',
  text: 'Bok',
  media: null,
  reply_to: null,
  forwarded: false,
  edited: false,
  deleted: false,
  system: false,
  reactions: [],
  poll: null,
  contact_cards: null,
  actionable: true,
  ticks: null,
}

const touch = { pointerType: 'touch', pointerId: 1 }

describe('Bubble gestures', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  function setup() {
    const onReply = vi.fn()
    const onMenu = vi.fn()
    const view = render(<Bubble message={message} showSender={false} tail onReply={onReply} onMenu={onMenu} />)
    return { onReply, onMenu, el: view.container.querySelector('.msg') as HTMLElement }
  }

  it('a swipe right replies', () => {
    const { onReply, onMenu, el } = setup()
    fireEvent.pointerDown(el, { ...touch, clientX: 10, clientY: 10 })
    fireEvent.pointerMove(el, { ...touch, clientX: 50, clientY: 12 })
    fireEvent.pointerUp(el, { ...touch, clientX: 80, clientY: 14 })
    expect(onReply).toHaveBeenCalledTimes(1)
    act(() => void vi.advanceTimersByTime(1000))
    expect(onMenu).not.toHaveBeenCalled()
  })

  it('a short drag or a vertical scroll does not reply', () => {
    const { onReply, el } = setup()
    fireEvent.pointerDown(el, { ...touch, clientX: 10, clientY: 10 })
    fireEvent.pointerUp(el, { ...touch, clientX: 40, clientY: 12 })
    fireEvent.pointerDown(el, { ...touch, clientX: 10, clientY: 10 })
    fireEvent.pointerUp(el, { ...touch, clientX: 80, clientY: 90 })
    expect(onReply).not.toHaveBeenCalled()
  })

  it('a long press opens the menu once, and moving first cancels it', () => {
    const { onMenu, el } = setup()
    fireEvent.pointerDown(el, { ...touch, clientX: 10, clientY: 10 })
    act(() => void vi.advanceTimersByTime(500))
    expect(onMenu).toHaveBeenCalledTimes(1)
    fireEvent.pointerUp(el, { ...touch, clientX: 10, clientY: 10 })
    fireEvent.pointerDown(el, { ...touch, clientX: 10, clientY: 10 })
    fireEvent.pointerMove(el, { ...touch, clientX: 10, clientY: 40 })
    act(() => void vi.advanceTimersByTime(900))
    expect(onMenu).toHaveBeenCalledTimes(1)
  })

  it('the mouse never starts a gesture, but right click opens the menu', () => {
    const { onReply, onMenu, el } = setup()
    fireEvent.pointerDown(el, { pointerType: 'mouse', clientX: 10, clientY: 10 })
    fireEvent.pointerUp(el, { pointerType: 'mouse', clientX: 90, clientY: 10 })
    expect(onReply).not.toHaveBeenCalled()
    fireEvent.contextMenu(el)
    expect(onMenu).toHaveBeenCalledTimes(1)
  })
})
