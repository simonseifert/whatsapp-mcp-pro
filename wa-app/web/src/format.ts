const DAY_MS = 24 * 60 * 60 * 1000

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
}

const two = (n: number) => String(n).padStart(2, '0')

/** Chat-list timestamp in the viewer's local time: today "21:14", yesterday "Yesterday", this week the weekday, else "12.11.". */
export function formatListTime(iso: string, now: Date = new Date()): string {
  const then = new Date(iso)
  if (Number.isNaN(then.getTime())) return ''
  // Calendar days, not 24-hour spans: 23:59 yesterday is "Yesterday" at 00:05 today. Rounding absorbs DST days.
  const days = Math.round((startOfDay(now) - startOfDay(then)) / DAY_MS)
  if (days <= 0) return `${two(then.getHours())}:${two(then.getMinutes())}`
  if (days === 1) return 'Yesterday'
  if (days < 7) return then.toLocaleDateString('en-GB', { weekday: 'long' })
  return `${two(then.getDate())}.${two(then.getMonth() + 1)}.`
}

const EXTRA_FOLD: Record<string, string> = { đ: 'd', ø: 'o', ł: 'l', ð: 'd', ı: 'i', ß: 'ss', æ: 'ae', œ: 'oe', þ: 'th' }

/** Lower-case without diacritics, so "durdica" finds "Đurđica" (stroked letters do not decompose). */
export function fold(text: string): string {
  const lower = text.toLowerCase().replace(/[đøłðıßæœþ]/g, (c) => EXTRA_FOLD[c] ?? c)
  return lower.normalize('NFKD').replace(/\p{M}/gu, '')
}

/** Up to two initials from the first letters of the first two words; '' when the name has no letters. */
export function initials(name: string): string {
  const words = name.split(/\s+/).filter((w) => /\p{L}/u.test(w))
  return words
    .slice(0, 2)
    .map((w) => (w.match(/\p{L}/u)?.[0] ?? '').toUpperCase())
    .join('')
}

/** A name that is only a phone number (nothing to take initials from). */
export function isNumberLike(name: string): boolean {
  return /^[+\d\s()-]*$/.test(name)
}
