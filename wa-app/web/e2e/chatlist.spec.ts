import { expect, test, type Page } from '@playwright/test'
import { signIn } from './helpers.js'

const noHorizontalScroll = (page: Page) =>
  page.evaluate<boolean>('document.documentElement.scrollWidth <= window.innerWidth')

test.beforeEach(async ({ page }) => {
  await signIn(page)
  await expect(page.locator('.row').first()).toBeVisible()
})

test('pinned chats come first, newest first', async ({ page }) => {
  const names = await page.locator('.row .name').allTextContents()
  expect(names.slice(0, 2)).toEqual(['Obitelj 🏡', 'Ana Kovačević'])
  expect(names).toContain('Tomo (posao)') // one chat for both halves of the split person
  expect(names.filter((n) => n.startsWith('Tomo'))).toHaveLength(1)
  await expect(page.getByLabel('2 unread', { exact: true })).toBeVisible()
  await expect(page.getByText('🎤 Voice message (0:42)')).toBeVisible()
})

for (const width of [320, 375]) {
  test(`a very long name truncates without horizontal scroll at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 })
    const long = page.locator('.row .name', { hasText: 'Đurđica Šimunović-Petrović' })
    await expect(long).toBeVisible()
    expect(await long.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true) // clipped with an ellipsis
    expect(await noHorizontalScroll(page)).toBe(true)
  })
}

test('filters, search without diacritics, and the archived list', async ({ page }) => {
  await page.getByRole('button', { name: 'Favourites' }).click()
  await expect(page.locator('.row .name')).toHaveText(['Obitelj 🏡', 'Ana Kovačević'])
  await page.getByRole('button', { name: 'All' }).click()

  await page.getByRole('searchbox').fill('durdica')
  await expect(page.locator('.row .name')).toHaveText([/^Đurđica/])
  await page.getByRole('searchbox').fill('')

  await page.getByRole('link', { name: /^Archived\s*2/ }).click()
  await expect(page).toHaveURL(/\/archived$/)
  await expect(page.getByRole('heading', { name: 'Archived' })).toBeVisible()
  await expect(page.locator('.row .name')).toHaveText(['Stara Kolegica', 'Stari tim'])
  await page.getByRole('link', { name: 'Chats' }).first().click()
  await expect(page.getByRole('heading', { name: 'Chats' })).toBeVisible()
})

test('a search by number keeps the chats the server matched', async ({ page }) => {
  await page.getByRole('searchbox').fill('4915112345678')
  await expect(page.locator('.row .name')).toHaveText(['Tomo (posao)'])
  await page.waitForTimeout(600) // past the debounce: the server answer must not be re-filtered by name
  await expect(page.locator('.row .name')).toHaveText(['Tomo (posao)'])
})

test('tab bar: later-version tabs and settings', async ({ page }) => {
  await page.getByRole('link', { name: 'Communities' }).click()
  await expect(page.getByText('Coming in a later version.')).toBeVisible()
  await page.getByRole('link', { name: 'Calls' }).click()
  await expect(page.getByText('Coming in a later version.')).toBeVisible()
  await page.getByRole('link', { name: 'Settings' }).click()
  await expect(page.getByRole('button', { name: 'Log out', exact: true })).toBeVisible()
})

test('two panes from 900px', async ({ page }) => {
  await page.setViewportSize({ width: 1100, height: 800 })
  await expect(page.getByText('Select a chat')).toBeVisible()
  await expect(page.locator('.row').first()).toBeVisible()
  expect(await noHorizontalScroll(page)).toBe(true)
})
