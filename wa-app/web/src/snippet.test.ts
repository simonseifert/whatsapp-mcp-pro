import { describe, expect, it } from 'vitest'
import { snippetParts } from './snippet'

describe('snippetParts', () => {
  it('splits matched words from plain text', () => {
    expect(snippetParts('Šaljem vam [račun] za listopad')).toEqual([
      { text: 'Šaljem vam ', hit: false },
      { text: 'račun', hit: true },
      { text: ' za listopad', hit: false },
    ])
  })
  it('handles a match at the start, several matches and an ellipsis', () => {
    expect(snippetParts('[a] b [c]…')).toEqual([
      { text: 'a', hit: true },
      { text: ' b ', hit: false },
      { text: 'c', hit: true },
      { text: '…', hit: false },
    ])
  })
  it('keeps an unclosed bracket as text', () => {
    expect(snippetParts('x [ y')).toEqual([{ text: 'x [ y', hit: false }])
  })
  it('never turns markup into anything but text', () => {
    expect(snippetParts('<img src=x onerror=alert(1)> [<b>hi</b>]')).toEqual([
      { text: '<img src=x onerror=alert(1)> ', hit: false },
      { text: '<b>hi</b>', hit: true },
    ])
  })
  it('is empty for an empty snippet', () => {
    expect(snippetParts('')).toEqual([])
  })
})
