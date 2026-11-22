export type Filter = 'all' | 'unread' | 'favourites' | 'groups'

const CHIPS: { id: Filter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'unread', label: 'Unread' },
  { id: 'favourites', label: 'Favourites' },
  { id: 'groups', label: 'Groups' },
]

export function FilterChips({ value, onChange }: { value: Filter; onChange: (filter: Filter) => void }) {
  return (
    <div className="chips" role="group" aria-label="Filter">
      {CHIPS.map((chip) => (
        <button key={chip.id} type="button" className="chip" aria-pressed={value === chip.id} onClick={() => onChange(chip.id)}>
          {chip.label}
        </button>
      ))}
    </div>
  )
}
