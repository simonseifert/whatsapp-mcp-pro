import { useState } from 'react'
import { initials, isNumberLike } from '../format'
import { Icon } from './Icon'

type Props = { name: string; src?: string; kind?: string }

/** Profile picture when the server has one; otherwise initials, or an icon for channels and bare numbers. */
export function Avatar({ name, src, kind }: Props) {
  const [failed, setFailed] = useState(false)
  if (src && !failed) {
    return <img className="av" src={src} alt="" loading="lazy" onError={() => setFailed(true)} />
  }
  const letters = initials(name)
  let content
  if (kind === 'announcement' || kind === 'newsletter') content = <Icon name="megaphone" size={22} />
  else if (letters === '' || isNumberLike(name)) content = <Icon name="person" size={22} />
  else content = letters
  return (
    <div className="av" aria-hidden="true">
      {content}
    </div>
  )
}
