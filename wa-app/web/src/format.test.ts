import { describe, expect, it } from 'vitest'
import { fold, formatDayLabel, formatDuration, formatListTime, initials, isNumberLike, linkParts, telHref } from './format'

// Local-time constructors so the cases hold in any timezone the tests run in.
const at = (y: number, m: number, d: number, h = 0, min = 0) => new Date(y, m - 1, d, h, min)
const iso = (d: Date) => d.toISOString()
const NOW = at(2026, 11, 22, 21, 30) // a Sunday

describe('formatListTime', () => {
  it('shows the clock time for today', () => {
    expect(formatListTime(iso(at(2026, 11, 22, 21, 14)), NOW)).toBe('21:14')
    expect(formatListTime(iso(at(2026, 11, 22, 7, 5)), NOW)).toBe('07:05')
  })

  it('says Yesterday for the previous calendar day', () => {
    expect(formatListTime(iso(at(2026, 11, 21, 9, 0)), NOW)).toBe('Yesterday')
  })

  it('uses the weekday for the rest of the week', () => {
    expect(formatListTime(iso(at(2026, 11, 20, 12, 0)), NOW)).toBe('Friday')
    expect(formatListTime(iso(at(2026, 11, 16, 12, 0)), NOW)).toBe('Monday') // six days back
  })

  it('uses dd.mm. from a week back', () => {
    expect(formatListTime(iso(at(2026, 11, 15, 12, 0)), NOW)).toBe('15.11.')
    expect(formatListTime(iso(at(2026, 11, 2, 12, 0)), NOW)).toBe('02.11.')
    expect(formatListTime(iso(at(2025, 12, 31, 12, 0)), NOW)).toBe('31.12.')
  })

  it('switches day at local midnight', () => {
    const justAfterMidnight = at(2026, 11, 22, 0, 5)
    expect(formatListTime(iso(at(2026, 11, 21, 23, 59)), justAfterMidnight)).toBe('Yesterday')
    expect(formatListTime(iso(at(2026, 11, 22, 0, 0)), justAfterMidnight)).toBe('00:00')
    const lastMinute = at(2026, 11, 22, 23, 59)
    expect(formatListTime(iso(at(2026, 11, 22, 0, 0)), lastMinute)).toBe('00:00')
    expect(formatListTime(iso(at(2026, 11, 21, 23, 59)), lastMinute)).toBe('Yesterday')
  })

  it('shows the time for a timestamp slightly in the future and nothing for garbage', () => {
    expect(formatListTime(iso(at(2026, 11, 22, 21, 31)), NOW)).toBe('21:31')
    expect(formatListTime('not a date', NOW)).toBe('')
  })
})

describe('fold', () => {
  it('matches with and without diacritics', () => {
    expect(fold('Đurđica Šimunović')).toBe('durdica simunovic')
    expect(fold('DURDICA')).toBe('durdica')
    expect(fold('Straße')).toBe('strasse')
  })
})

describe('initials', () => {
  it('takes two letters, skipping emoji and numbers', () => {
    expect(initials('Ana Kovačević')).toBe('AK')
    expect(initials('Nikola')).toBe('N')
    expect(initials('Obitelj 🏡')).toBe('O')
    expect(initials('Đurđica Šimunović-Petrović (Računovodstvo d.o.o.)')).toBe('ĐŠ')
    expect(initials('+385 91 000 0000')).toBe('')
  })

  it('recognises number-only names', () => {
    expect(isNumberLike('+385 91 000 0000')).toBe(true)
    expect(isNumberLike('Ana')).toBe(false)
  })
})

describe('chat formatting', () => {
  const now = new Date(2026, 10, 22, 12, 0)
  it('labels days', () => {
    expect(formatDayLabel(new Date(2026, 10, 22, 1, 0).toISOString(), now)).toBe('Today')
    expect(formatDayLabel(new Date(2026, 10, 21, 23, 0).toISOString(), now)).toBe('Yesterday')
    expect(formatDayLabel(new Date(2026, 10, 19, 9, 0).toISOString(), now)).toBe('Thursday')
    expect(formatDayLabel(new Date(2026, 9, 1, 9, 0).toISOString(), now)).toBe('1 Oct 2026')
  })
  it('finds links and keeps trailing punctuation out of them', () => {
    expect(linkParts('see https://example.org/a?b=1. ok')).toEqual([
      { text: 'see ' },
      { text: 'https://example.org/a?b=1', href: 'https://example.org/a?b=1' },
      { text: '. ok' },
    ])
    expect(linkParts('(http://x.io)')).toEqual([{ text: '(' }, { text: 'http://x.io', href: 'http://x.io' }, { text: ')' }])
  })
  it('never makes a link of markup or other schemes', () => {
    expect(linkParts('<b>hi</b> javascript:alert(1)')).toEqual([{ text: '<b>hi</b> javascript:alert(1)' }])
    expect(linkParts('https://')).toEqual([{ text: 'https://' }])
  })
  it('formats durations and phone targets', () => {
    expect(formatDuration(37)).toBe('0:37')
    expect(formatDuration(605)).toBe('10:05')
    expect(telHref('+385 (91) 000-0777')).toBe('tel:+385910000777')
    expect(telHref('n/a')).toBeNull()
    expect(telHref('+')).toBeNull()
  })
})
