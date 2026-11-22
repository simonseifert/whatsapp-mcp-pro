import { afterEach, describe, expect, it } from 'vitest'
import { applyTheme, readTheme, resolveTheme, saveTheme, THEME_KEY } from './theme'

afterEach(() => {
  localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
})

describe('resolveTheme', () => {
  it('follows the system for "system"', () => {
    expect(resolveTheme('system', true)).toBe('dark')
    expect(resolveTheme('system', false)).toBe('light')
  })
  it('an explicit choice wins over the system', () => {
    expect(resolveTheme('dark', false)).toBe('dark')
    expect(resolveTheme('light', true)).toBe('light')
  })
})

describe('stored preference', () => {
  it('defaults to system, also for junk', () => {
    expect(readTheme()).toBe('system')
    localStorage.setItem(THEME_KEY, 'neon')
    expect(readTheme()).toBe('system')
  })
  it('saving dark or light sets data-theme and persists', () => {
    saveTheme('light')
    expect(readTheme()).toBe('light')
    expect(document.documentElement.dataset.theme).toBe('light')
    saveTheme('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
  })
  it('saving system removes data-theme so the media query decides', () => {
    saveTheme('dark')
    saveTheme('system')
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false)
    expect(localStorage.getItem(THEME_KEY)).toBe('system')
  })
  it('applyTheme restores the stored choice', () => {
    localStorage.setItem(THEME_KEY, 'light')
    applyTheme()
    expect(document.documentElement.dataset.theme).toBe('light')
  })
  it('survives a storage that throws', () => {
    const real = Storage.prototype.getItem
    Storage.prototype.getItem = () => {
      throw new Error('denied')
    }
    try {
      expect(readTheme()).toBe('system')
    } finally {
      Storage.prototype.getItem = real
    }
  })
})
