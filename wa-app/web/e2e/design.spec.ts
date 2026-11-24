/// <reference lib="dom" />
import { expect, test, type Locator, type Page } from '@playwright/test'
import { signIn } from './helpers.js'

const style = (target: Locator, property: string): Promise<string> =>
  target.evaluate((el, name) => window.getComputedStyle(el).getPropertyValue(name), property)
const NEUTRAL_FG = async (page: Page, selector: string) => {
  const color = await style(page.locator(selector).first(), 'color')
  expect(color).toMatch(/^rgb/) // a real computed colour, so the "not amber" checks below cannot pass on nothing
  return color
}

/** Amber is rgb(240, 169, 74) in dark and rgb(147, 84, 0) / rgb(163, 94, 5) in light: none may reach these controls. */
const AMBER = ['rgb(240, 169, 74)', 'rgb(147, 84, 0)', 'rgb(163, 94, 5)']

async function openEkipa(page: Page) {
  await signIn(page)
  await page.locator('.row .name', { hasText: 'Ekipa za vikend' }).click()
  await expect(page.locator('.msg').first()).toBeVisible()
}

test('header icons are neutral and the menu hangs off the kebab at the top right', async ({ page }) => {
  await openEkipa(page)
  for (const sel of ['.chead .iconbtn:last-child']) expect(AMBER).not.toContain(await NEUTRAL_FG(page, sel))
  await page.getByRole('button', { name: 'Chat menu' }).click()
  const menu = await page.locator('.menu').boundingBox()
  const kebab = await page.getByRole('button', { name: 'Chat menu' }).boundingBox()
  const viewport = page.viewportSize()
  if (!menu || !kebab || !viewport) throw new Error('no boxes')
  expect(menu.y).toBeLessThan(kebab.y + kebab.height + 24)
  expect(menu.x + menu.width).toBeGreaterThan(viewport.width - 24)
  expect(menu.x + menu.width).toBeLessThanOrEqual(viewport.width)
})

test('the AI sheet has labelled 44 px switches and shows the warning', async ({ page }) => {
  await openEkipa(page)
  await page.getByRole('button', { name: 'Chat menu' }).click()
  await page.getByRole('button', { name: 'AI drafts' }).click()
  for (const name of ['Drafts in this chat', 'Local only']) {
    const toggle = page.getByRole('switch', { name })
    await expect(toggle).toBeVisible()
    const box = await toggle.boundingBox()
    expect(box?.height).toBeGreaterThanOrEqual(44)
    expect(await style(toggle, 'appearance')).toBe('none')
  }
  expect(AMBER).not.toContain(await NEUTRAL_FG(page, '.sheet .iconbtn'))
  const local = page.getByRole('switch', { name: 'Local only' })
  await local.check()
  const warning = page.getByRole('alert').filter({ hasText: 'Add a local AI in Settings first' })
  await expect(warning).toBeVisible() // the sandbox has no local AI, so Local only must warn
  expect(await style(warning, 'color')).not.toBe(await NEUTRAL_FG(page, '.sheet h2'))
  await expect(warning.locator('svg')).toBeVisible()
})

test('the draft card stays inside the 760 px column on desktop', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.route('**/api/chats/*/draft', (route) =>
    route.request().method() === 'GET'
      ? route.fulfill({
          json: {
            chat_jid: 'x',
            draft: {
              id: 1,
              chat_jid: 'x',
              trigger_id: 't',
              status: 'ready',
              alternatives: ['Stižem za deset minuta'],
              error: null,
              quote: null,
              created_at_iso: new Date().toISOString(),
              source: 'ai',
            },
          },
        })
      : route.continue(),
  )
  await openEkipa(page)
  const card = await page.locator('.draft-card').first().boundingBox()
  expect(card?.width).toBeLessThanOrEqual(760)
})

test('settings: Log out is neutral', async ({ page }) => {
  await signIn(page)
  await page.getByRole('link', { name: 'Settings' }).click()
  const logout = page.getByRole('button', { name: 'Log out', exact: true })
  await expect(logout).toBeVisible()
  expect(await style(logout, 'color')).toMatch(/^rgb/)
  expect(AMBER).not.toContain(await style(logout, 'color'))
})

test('the service worker caches the theme bootstrap too', async ({ page }) => {
  const sw = await (await page.request.get('/sw.js')).text()
  expect(sw).toContain('/theme-init.js')
})
