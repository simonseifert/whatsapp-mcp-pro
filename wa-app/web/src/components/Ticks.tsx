import type { MessageView } from '../api-types'
import { Icon } from './Icon'

const LABEL = { sent: 'Sent', delivered: 'Delivered', read: 'Read' } as const

/** Delivery state of an own message. Read is the full text colour, never an accent (DESIGN.md: one accent). */
export function Ticks({ state }: { state: MessageView['ticks'] }) {
  if (!state) return null
  return (
    <span className={`ticks ${state}`}>
      <Icon name={state === 'sent' ? 'check' : 'checks'} size={16} label={LABEL[state]} />
    </span>
  )
}
