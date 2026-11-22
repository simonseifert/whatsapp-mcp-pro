export function Placeholder({ title }: { title: string }) {
  return (
    <div className="pane-scroll">
      <header className="lhead">
        <h1 className="large-title">{title}</h1>
      </header>
      <p className="empty-state">Coming in a later version.</p>
    </div>
  )
}
