import { linkParts } from '../format'

/** Message text as React text nodes, with http(s) links as anchors. Never HTML. */
export function RichText({ text }: { text: string }) {
  return (
    <span className="txt" dir="auto">
      {linkParts(text).map((part, i) =>
        part.href ? (
          <a key={i} href={part.href} target="_blank" rel="noopener noreferrer">
            {part.text}
          </a>
        ) : (
          part.text
        ),
      )}
    </span>
  )
}
