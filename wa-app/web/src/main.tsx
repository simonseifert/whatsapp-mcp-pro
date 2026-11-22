import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './styles/app.css'
import { applyTheme, watchSystemTheme } from './theme'
import { watchForUpdate } from './update'

applyTheme()
watchSystemTheme()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

// Production only: the dev server has no shell files to cache.
if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  watchForUpdate()
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => undefined)
  })
}
