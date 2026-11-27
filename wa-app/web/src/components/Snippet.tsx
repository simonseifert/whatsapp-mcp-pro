import { snippetParts } from '../snippet'

/** A search snippet with the matched words marked. React text nodes only: whatever the message said stays text. */
export function Snippet({ text }: { text: string }) {
  return (
    <span className="snip" dir="auto">
      {snippetParts(text).map((part, i) => (part.hit ? <mark key={i}>{part.text}</mark> : part.text))}
    </span>
  )
}
