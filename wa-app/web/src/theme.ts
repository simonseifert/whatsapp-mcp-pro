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

/** Puts the stored choice on <html data-theme>. "System" leaves no attribute: the CSS media query decides. */
export function applyTheme(pref: ThemePref = readTheme()): void {
  const root = document.documentElement
  if (pref === 'system') root.removeAttribute('data-theme')
  else root.dataset.theme = pref
}

export function saveTheme(pref: ThemePref): void {
  try {
    localStorage.setItem(THEME_KEY, pref)
  } catch {
    // private mode: the choice lasts until reload
  }
  applyTheme(pref)
}
