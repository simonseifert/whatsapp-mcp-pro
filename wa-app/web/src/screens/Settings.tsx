export function Settings({ onLogout }: { onLogout: () => void }) {
  return (
    <div className="pane-scroll">
      <header className="lhead">
        <h1 className="large-title">Settings</h1>
      </header>
      <div className="settings">
        <button className="button-text" type="button" onClick={onLogout}>
          Log out
        </button>
      </div>
    </div>
  )
}
