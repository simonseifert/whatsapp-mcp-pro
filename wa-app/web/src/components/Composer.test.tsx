import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { MessageView } from '../api-types'
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

describe('Composer edit mode', () => {
  const own: MessageView = {
    id: 'M1',
    chat_jid: 'a@s.whatsapp.net',
    sender_jid: null,
    sender_name: null,
    from_me: true,
    timestamp_iso: '2026-11-27T10:00:00+00:00',
    text: 'Vidimo se u 7',
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

  function setup(extra: Record<string, unknown> = {}) {
    const onSend = vi.fn()
    const onSaveEdit = vi.fn()
    const onCancelEdit = vi.fn()
    render(
      <Composer
        replyTo={null}
        onCancelReply={() => {}}
        onSend={onSend}
        onAttach={() => {}}
        onVoice={() => {}}
        editing={own}
        onSaveEdit={onSaveEdit}
        onCancelEdit={onCancelEdit}
        {...extra}
      />,
    )
    return { onSend, onSaveEdit, onCancelEdit }
  }

  it('fills the box with the message, says it is an edit and flags the composer as editing', () => {
    setup()
    expect((screen.getByLabelText('Message') as HTMLTextAreaElement).value).toBe('Vidimo se u 7')
    expect(screen.getByText('Editing message')).toBeTruthy()
    expect(document.querySelector('.composer')?.getAttribute('data-editing')).toBe('true')
  })

  it('saves the changed text with the edit handler, never as a new message', () => {
    const { onSend, onSaveEdit } = setup()
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Vidimo se u 8' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save edit' }))
    expect(onSaveEdit).toHaveBeenCalledWith('Vidimo se u 8')
    expect(onSend).not.toHaveBeenCalled()
  })

  it('does not save an unchanged or emptied text', () => {
    const { onSaveEdit } = setup()
    expect((screen.getByRole('button', { name: 'Save edit' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: '  ' } })
    expect((screen.getByRole('button', { name: 'Save edit' }) as HTMLButtonElement).disabled).toBe(true)
    expect(onSaveEdit).not.toHaveBeenCalled()
  })

  it('cancel and Escape leave edit mode and empty the box; attach and mic are hidden meanwhile', () => {
    const { onCancelEdit } = setup()
    expect(screen.queryByRole('button', { name: 'Attach a photo or file' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel edit' }))
    fireEvent.keyDown(screen.getByLabelText('Message'), { key: 'Escape' })
    expect(onCancelEdit).toHaveBeenCalledTimes(2)
  })

  it('a normal composer is not flagged as editing', () => {
    render(<Composer replyTo={null} onCancelReply={() => {}} onSend={() => {}} />)
    expect(document.querySelector('.composer')?.getAttribute('data-editing')).toBe('false')
  })
})

describe('Composer quick replies', () => {
  const list = [
    { shortcut: 'thanks', text: 'Thanks a lot', used: 1 },
    { shortcut: 'omw', text: 'On my way', used: 0 },
  ]
  beforeEach(() => {
    vi.spyOn(api, 'fetchQuickReplies').mockResolvedValue(list)
    vi.spyOn(api, 'markQuickReplyUsed').mockResolvedValue(undefined)
  })
  afterEach(() => vi.restoreAllMocks())

  const box = () => screen.getByLabelText('Message') as HTMLTextAreaElement

  it('opens on a leading slash, filters as you type, inserts the text and never sends', async () => {
    const onSend = vi.fn()
    render(<Composer replyTo={null} onCancelReply={() => {}} onSend={onSend} />)
    fireEvent.change(box(), { target: { value: '/' } })
    expect(await screen.findAllByRole('option')).toHaveLength(2)
    fireEvent.change(box(), { target: { value: '/om' } })
    expect(screen.getAllByRole('option')).toHaveLength(1)
    fireEvent.click(screen.getByRole('option', { name: /omw/ }))
    expect(box().value).toBe('On my way')
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(onSend).not.toHaveBeenCalled()
    expect(api.markQuickReplyUsed).toHaveBeenCalledWith('omw')
  })

  it('does not open for a slash in the middle of the text', () => {
    render(<Composer replyTo={null} onCancelReply={() => {}} onSend={() => {}} />)
    fireEvent.change(box(), { target: { value: 'a/b' } })
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(api.fetchQuickReplies).not.toHaveBeenCalled()
  })

  it('closes on Escape and when the text has a space', async () => {
    render(<Composer replyTo={null} onCancelReply={() => {}} onSend={() => {}} />)
    fireEvent.change(box(), { target: { value: '/' } })
    await screen.findByRole('listbox')
    fireEvent.keyDown(box(), { key: 'Escape' })
    expect(screen.queryByRole('listbox')).toBeNull()
    fireEvent.change(box(), { target: { value: '' } })
    fireEvent.change(box(), { target: { value: '/t' } })
    await screen.findByRole('listbox')
    fireEvent.change(box(), { target: { value: '/t x' } })
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('stays closed in edit mode, even when the edited text starts with a slash', () => {
    const own = { id: 'm1', from_me: true, text: '/thanks', ts: '2026-01-01T00:00:00Z' } as unknown as MessageView
    render(<Composer replyTo={null} onCancelReply={() => {}} onSend={() => {}} editing={own} onSaveEdit={() => {}} />)
    expect(box().value).toBe('/thanks')
    fireEvent.change(box(), { target: { value: '/' } })
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(api.fetchQuickReplies).not.toHaveBeenCalled()
  })
})
