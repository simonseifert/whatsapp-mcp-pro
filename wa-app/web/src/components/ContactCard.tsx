import type { ContactCardView } from '../api-types'
import { initials, telHref } from '../format'
import { Icon } from './Icon'

/** A shared contact: initials, name and each phone number as a tap-to-call link. */
export function ContactCard({ card }: { card: ContactCardView }) {
  return (
    <div className="contact">
      <div className="contact-head">
        <div className="av sm" aria-hidden="true">
          {initials(card.name) || <Icon name="person" size={18} />}
        </div>
        <span className="contact-name" dir="auto">
          {card.name}
        </span>
      </div>
      {card.phones.map((phone) => {
        const href = telHref(phone)
        const inner = (
          <>
            <Icon name="phone" size={16} />
            <span>{phone}</span>
          </>
        )
        // A "number" with no digits is not dialable: show it as text, not a bare tel: link.
        return href ? (
          <a className="contact-phone" key={phone} href={href}>
            {inner}
          </a>
        ) : (
          <span className="contact-phone" key={phone}>
            {inner}
          </span>
        )
      })}
    </div>
  )
}
