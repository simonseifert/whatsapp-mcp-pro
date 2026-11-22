export type ThemePref = 'system' | 'dark' | 'light'

export const THEME_KEY = 'wa-theme'
export const THEMES: { value: ThemePref; label: string }[] = [
  { value: 'system', label: 'System' },
  { value: 'dark', label: 'Dark' },
  { value: 'light', label: 'Light' },
]

/** The theme actually shown: an explicit choice, or whatever the system prefers. */
export function resolveTheme(pref: ThemePref, systemDark: boolean): 'dark' | 'light' {
  return pref === 'system' ? (systemDark ? 'dark' : 'light') : pref
}

export function readTheme(): ThemePref {
  try {
    const stored = localStorage.getItem(THEME_KEY)
    return stored === 'dark' || stored === 'light' ? stored : 'system'
  } catch {
    return 'system'
  }
}

export const THEME_COLOR = { dark: '#0b0b0c', light: '#f7f5f1' }

const systemDark = () => window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? true

/** Puts the stored choice on <html data-theme>. "System" leaves no attribute: the CSS media query decides. */
export function applyTheme(pref: ThemePref = readTheme()): void {
  const root = document.documentElement
  if (pref === 'system') root.removeAttribute('data-theme')
  else root.dataset.theme = pref
  // One theme-color for browser chrome, from the resolved theme (a chosen Light on a dark OS must not stay dark).
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', THEME_COLOR[resolveTheme(pref, systemDark())])
}

/** While the choice is "System", a change of the OS theme updates the theme-color meta. */
export function watchSystemTheme(): void {
  window.matchMedia?.('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (readTheme() === 'system') applyTheme('system')
  })
}

export function saveTheme(pref: ThemePref): void {
  try {
    localStorage.setItem(THEME_KEY, pref)
  } catch {
    // private mode: the choice lasts until reload
  }
  applyTheme(pref)
}
