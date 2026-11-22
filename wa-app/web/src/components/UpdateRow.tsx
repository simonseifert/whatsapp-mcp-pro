import { useUpdateAvailable } from '../update'

/** "Update available · Reload": `banner` is the slim strip above the list, otherwise the row inside Settings. */
export function UpdateRow({ banner = false }: { banner?: boolean }) {
  if (!useUpdateAvailable()) return null
  return (
    <div className={banner ? 'update-row banner' : 'update-row'} role="status">
      <span>Update available</span>
      <span aria-hidden="true">·</span>
      <button className="button-text" type="button" onClick={() => window.location.reload()}>
        Reload
      </button>
    </div>
  )
}
