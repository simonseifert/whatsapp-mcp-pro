import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AttachSheet } from './AttachSheet'
import { Bubble } from './Bubble'
import type { MessageView } from '../api-types'

afterEach(cleanup)

const quote = { id: 'M7', sender_name: 'Ana', preview: 'Pošalji račun' }

function fileOf(size: number, name = 'Račun.pdf', type = 'application/pdf'): File {
  const f = new File(['x'], name, { type })
  Object.defineProperty(f, 'size', { value: size })
  return f
}

describe('AttachSheet', () => {
  it('shows the size limit message for a 65 MB file', () => {
    const onSend = vi.fn()
    render(<AttachSheet file={fileOf(65 * 1024 * 1024)} replyTo={null} onSend={onSend} onClose={() => {}} />)
    expect(screen.getByRole('alert').textContent).toContain('Too big to send (max 64 MB)')
    const send = screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement
    expect(send.disabled).toBe(true)
    fireEvent.click(send)
    expect(onSend).not.toHaveBeenCalled()
  })

  it('sends caption and reply id', () => {
    const onSend = vi.fn()
    const file = fileOf(2048)
    render(<AttachSheet file={file} replyTo={quote} onSend={onSend} onClose={() => {}} />)
    fireEvent.change(screen.getByLabelText('Caption'), { target: { value: '  Evo računa, hvala  ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(onSend).toHaveBeenCalledTimes(1)
    const [sentFile, caption, replyTo] = onSend.mock.calls[0]
    expect(sentFile).toBe(file)
    expect(caption).toBe('Evo računa, hvala')
    expect(replyTo.id).toBe('M7')
  })

  it('blocks a caption that is too long once encoded', () => {
    render(<AttachSheet file={fileOf(10)} replyTo={null} onSend={() => {}} onClose={() => {}} />)
    fireEvent.change(screen.getByLabelText('Caption'), { target: { value: 'š'.repeat(1000) } })
    expect(screen.getByRole('alert').textContent).toContain('Caption is too long')
    expect((screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('shows a file row with name and size for a non-image', () => {
    render(<AttachSheet file={fileOf(734_003, 'plan.pdf')} replyTo={null} onSend={() => {}} onClose={() => {}} />)
    expect(screen.getByText('plan.pdf')).toBeTruthy()
    expect(screen.getByText('717 KB')).toBeTruthy()
  })

  it('wraps a long Croatian file name instead of overflowing', () => {
    const name = `${'Ugovor o zakupu poslovnog prostora čćšžđ '.repeat(4)}konačna verzija.pdf`
    render(<AttachSheet file={fileOf(1000, name)} replyTo={null} onSend={() => {}} onClose={() => {}} />)
    const el = screen.getByText(name)
    expect(el.className).toContain('attach-name') // styled overflow-wrap: anywhere, min-width: 0
    expect(el.getAttribute('dir')).toBe('auto')
  })
})

describe('Bubble upload row', () => {
  const m: MessageView = {
    id: 'pending:k',
    chat_jid: 'a@s.whatsapp.net',
    sender_jid: null,
    sender_name: null,
    from_me: true,
    timestamp_iso: '2026-11-25T10:00:00+00:00',
    text: 'Evo',
    media: null,
    reply_to: null,
    forwarded: false,
    edited: false,
    deleted: false,
    system: false,
    reactions: [],
    poll: null,
    contact_cards: null,
    actionable: false,
    ticks: null,
  }

  it('shows the name and a progress line while sending', () => {
    render(
      <Bubble
        message={m}
        showSender={false}
        tail
        delivery={{ state: 'sending', upload: { name: 'Račun.pdf', size: 1000, progress: 0.4 } }}
      />,
    )
    expect(screen.getByText('Račun.pdf')).toBeTruthy()
    expect(screen.getByRole('progressbar', { name: 'Uploading, 40%' })).toBeTruthy()
  })

  it('shows Retry when the upload failed', () => {
    const onRetry = vi.fn()
    render(
      <Bubble
        message={m}
        showSender={false}
        tail
        delivery={{ state: 'failed', error: "Not sent: bridge didn't answer", retryable: true, upload: { name: 'a.pdf', size: 1, progress: 1 } }}
        onRetry={onRetry}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRetry).toHaveBeenCalled()
  })
})
