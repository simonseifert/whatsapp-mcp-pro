import { expect, test, type Page } from '@playwright/test'
import { signIn } from './helpers.js'

const noHorizontalScroll = (page: Page) =>
  page.evaluate<boolean>('document.documentElement.scrollWidth <= window.innerWidth')

const box = async (page: Page, selector: string) => {
  const b = await page.locator(selector).first().boundingBox()
  if (!b) throw new Error(`${selector} has no box`)
  return b
}

test('1440 px shows the list and the chat side by side, the column capped at 760', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await signIn(page)
  await expect(page.getByText('Select a chat')).toBeVisible()
  const list = await box(page, '.pane-list')
  expect(Math.round(list.width)).toBe(380)

  await page.locator('.row .name', { hasText: 'Ekipa za vikend' }).click()
  await expect(page).toHaveURL(/\/chat\//)
  await expect(page.locator('.msg').first()).toBeVisible()
  await expect(page.locator('.row').first()).toBeVisible() // the list stays
  await expect(page.locator('.back-btn')).toBeHidden()

  // Inside the scroll area the first message's bubble sits within a 760 px column centred in the chat pane.
  const pane = await box(page, '.chatpane')
  const bubble = await box(page, '.msg .bubble')
  expect(bubble.x).toBeGreaterThanOrEqual(pane.x + (pane.width - 760) / 2 - 1)
  expect(bubble.x + bubble.width).toBeLessThanOrEqual(pane.x + (pane.width + 760) / 2 + 1)
  const composer = await box(page, '.composer-row')
  expect(composer.width).toBeLessThanOrEqual(760)

  // Picking another chat swaps the right pane; the URL carries the chat in both layouts.
  await page.locator('.row .name', { hasText: 'Marta Dugi' }).click()
  await expect(page.getByRole('heading', { name: 'Marta Dugi' })).toBeVisible()
})

test('375 px is one pane and back returns to the list at the same scroll position', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 460 })
  await signIn(page)
  await expect(page.locator('.row').first()).toBeVisible()
  const scrolled = await page.evaluate<number>(`(() => {
    const el = document.querySelector('.pane-list .pane-scroll')
    el.scrollTop = 150
    return el.scrollTop
  })()`)
  expect(scrolled).toBeGreaterThan(0)

  await page.locator('.row:visible').nth(3).click()
  await expect(page).toHaveURL(/\/chat\//)
  await expect(page.locator('.msg').first()).toBeVisible()
  await expect(page.locator('.pane-list')).toBeHidden() // covered and out of focus order
  expect((await box(page, '.pane-detail')).x).toBe(0)

  await page.getByRole('link', { name: 'Back to chats' }).click()
  await expect(page.getByRole('heading', { name: 'Chats' })).toBeVisible()
  await expect(page.locator('.pane-detail')).toBeHidden()
  const after = await page.evaluate<number>(`document.querySelector('.pane-list .pane-scroll').scrollTop`)
  expect(after).toBe(scrolled)
  expect(await noHorizontalScroll(page)).toBe(true)
})

test('the chat slides over on a phone, and cross-fades with reduced motion', async ({ page }) => {
  await signIn(page)
  const transform = () => page.evaluate<string>(`getComputedStyle(document.querySelector('.pane-detail')).transform`)
  const paneX = () => page.evaluate<number>(`document.querySelector('.pane-detail').getBoundingClientRect().x`)
  const open = async () => {
    await page.locator('.row .name', { hasText: 'Ekipa za vikend' }).click()
    await expect(page.locator('.msg').first()).toBeVisible()
  }
  // Mid-slide the pane sits right of the screen edge: sample it right after the tap.
  await page.locator('.row .name', { hasText: 'Ekipa za vikend' }).click()
  await page.evaluate('new Promise((r) => setTimeout(r, 0))')
  expect(await paneX()).toBeGreaterThan(0)
  await expect.poll(transform).toBe('none') // settled after the slide
  await expect(page.locator('.msg').first()).toBeVisible()

  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.getByRole('link', { name: 'Back to chats' }).click()
  await expect(page.locator('.pane-detail')).toBeHidden()
  await open()
  // No translate at any point: it fades in place.
  expect(await transform()).toBe('none')
  expect(await paneX()).toBe(0)
})

test('reduced motion: opening a chat never moves the pane', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await signIn(page)
  await page.locator('.row .name', { hasText: 'Ekipa za vikend' }).click()
  expect(await page.evaluate<number>(`document.querySelector('.pane-detail').getBoundingClientRect().x`)).toBe(0)
})

test('theme choice applies at once, persists over a reload and is set before first paint', async ({ page }) => {
  await signIn(page)
  await page.getByRole('link', { name: 'Settings' }).click()
  await page.getByLabel('Light').check()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  expect(await page.evaluate<string>(`getComputedStyle(document.documentElement).colorScheme`)).toBe('light')

  await page.reload()
  await expect(page.getByLabel('Light')).toBeChecked()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')

  // The inline script in index.html (not the app bundle) sets it first: it is in the served HTML.
  const html = await (await page.request.get('/')).text()
  expect(html).toContain("localStorage.getItem('wa-theme')")

  await page.getByLabel('Dark').check()
  await page.reload()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await page.getByLabel('System').check()
  await expect(page.locator('html')).not.toHaveAttribute('data-theme', /.*/)
})

for (const theme of ['dark', 'light'] as const) {
  test(`${theme} theme has no horizontal overflow at 320 px`, async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 640 })
    await page.addInitScript(`localStorage.setItem('wa-theme', '${theme}')`)
    await signIn(page)
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme)
    expect(await noHorizontalScroll(page)).toBe(true)
    await page.getByRole('link', { name: 'Settings' }).click()
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
    expect(await noHorizontalScroll(page)).toBe(true)
    await page.getByRole('link', { name: 'Chats' }).click()
    await page.locator('.row .name', { hasText: 'Ekipa za vikend' }).click()
    await expect(page.locator('.msg').first()).toBeVisible()
    expect(await noHorizontalScroll(page)).toBe(true)
  })
}

test('settings shows the version', async ({ page }) => {
  await signIn(page)
  await page.getByRole('link', { name: 'Settings' }).click()
  await expect(page.getByText(/^wa-app \S+/)).toBeVisible()
})

test('log out everywhere ends the other sessions too', async ({ page, browser, baseURL }) => {
  await signIn(page)
  const other = await browser.newContext({ baseURL })
  const phone = await other.newPage()
  await signIn(phone)

  await page.getByRole('link', { name: 'Settings' }).click()
  await page.getByRole('button', { name: 'Log out everywhere' }).click()
  await page.getByRole('button', { name: 'Yes, log out everywhere' }).click()
  await expect(page.getByLabel('Passphrase')).toBeVisible()

  const status = await phone.evaluate<number>(`fetch('/api/chats').then((r) => r.status)`)
  expect(status).toBe(401)
  await other.close()
})

test.describe('service worker', () => {
  test.use({ serviceWorkers: 'allow' })

  test('caches the shell and never /api', async ({ page }) => {
    await signIn(page)
    await page.evaluate('navigator.serviceWorker.ready.then(() => true)')
    await page.reload() // controlled by the worker from here on
    await expect(page.getByRole('heading', { name: 'Chats' })).toBeVisible()
    await page.locator('.row .name').first().click()
    await expect(page.locator('.msg').first()).toBeVisible()
    const cached = await page.evaluate<string[]>(`(async () => {
      const urls = []
      for (const name of await caches.keys())
        for (const req of await (await caches.open(name)).keys()) urls.push(new URL(req.url).pathname)
      return urls
    })()`)
    expect(cached).toContain('/')
    expect(cached.some((u) => u.endsWith('.js'))).toBe(true)
    expect(cached.some((u) => u.endsWith('.css'))).toBe(true)
    expect(cached.some((u) => u.startsWith('/api/'))).toBe(false)
  })
})
