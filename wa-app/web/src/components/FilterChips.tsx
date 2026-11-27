import type { FilterChip } from '../api-types'

/** `all`, `unread`, `favourites`, `groups`, `mentions`, or `list:<id>`: the server defines them (see /api/chat-filters). */
export type Filter = string

/** Shown until the server's chips arrive, so the row never jumps in empty. */
export const BUILT_IN_CHIPS: FilterChip[] = [
  { id: 'all', label: 'All' },
  { id: 'unread', label: 'Unread' },
  { id: 'favourites', label: 'Favourites' },
  { id: 'groups', label: 'Groups' },
]

export function FilterChips({
  chips,
  value,
  onChange,
}: {
  chips: FilterChip[] | null
  value: Filter
  onChange: (filter: Filter) => void
}) {
  return (
    <div className="chips" role="group" aria-label="Filter">
      {(chips ?? BUILT_IN_CHIPS).map((chip) => (
        <button key={chip.id} type="button" className="chip" aria-pressed={value === chip.id} onClick={() => onChange(chip.id)}>
          {chip.label}
        </button>
      ))}
    </div>
  )
}
