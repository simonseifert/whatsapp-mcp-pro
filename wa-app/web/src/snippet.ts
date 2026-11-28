/** Fewer letters than this are not searched (the server agrees). */
export const MIN_QUERY = 2

export type SnippetPart = { text: string; hit: boolean }

/**
 * Splits a search snippet into plain text and matched words. The server marks a match with `[` and `]`; the parts are
 * rendered as React text (the matched ones in <mark>), never as HTML. A `[` with no closing `]` is just text.
 */
export function snippetParts(snippet: string): SnippetPart[] {
  const parts: SnippetPart[] = []
  let rest = snippet
  while (rest) {
    const open = rest.indexOf('[')
    const close = open < 0 ? -1 : rest.indexOf(']', open + 1)
    if (close < 0) {
      parts.push({ text: rest, hit: false })
      break
    }
    if (open > 0) parts.push({ text: rest.slice(0, open), hit: false })
    if (close > open + 1) parts.push({ text: rest.slice(open + 1, close), hit: true })
    rest = rest.slice(close + 1)
  }
  return parts
}
