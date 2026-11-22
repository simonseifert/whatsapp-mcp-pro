import { expect, test, type Page } from '@playwright/test'
import { signIn } from './helpers.js'
import { FakeBridge, LUKAS, MARTA } from './store.js'

let bridge: FakeBridge
test.beforeEach(() => {
  bridge = new FakeBridge()
})
test.afterEach(() => bridge.cleanup())

async function openChat(page: Page, name: string) {
  await signIn(page)
  await page.locator('.row .name', { hasText: name }).click()
  await expect(page).toHaveURL(/\/chat\//)
  await expect(page.locator('.msg').first()).toBeVisible()
}

test('a new message appears in the open chat within 3 s and rises in', async ({ page }) => {
  await openChat(page, 'Lukas Brenner')
  const before = await page.locator('.msg').count()
  bridge.insert(LUKAS, 'LIVE1', 'Stigla je nova poruka uživo')
  const bubble = page.locator('.msg', { hasText: 'Stigla je nova poruka uživo' })
  await expect(bubble).toBeVisible({ timeout: 3000 })
  await expect(page.locator('.msg')).toHaveCount(before + 1)
  await expect(page.locator('.msg').last()).toContainText('Stigla je nova poruka uživo')
  await expect(bubble).toHaveClass(/rise/)
  await expect(page.locator('.newpill')).toHaveCount(0)
})

test('a history message lands by its time, with no animation and no pill', async ({ page }) => {
  await openChat(page, 'Lukas Brenner')
  // Lukas's messages are about 400 minutes old: this one is older, so it belongs above them, not at the end.
  bridge.insert(LUKAS, 'LIVE2', 'Poruka iz povijesti', 500)
  const bubble = page.locator('.msg', { hasText: 'Poruka iz povijesti' })
  await expect(bubble).toBeVisible({ timeout: 3000 })
  await expect(page.locator('.msg').last()).not.toContainText('Poruka iz povijesti')
  await expect(page.locator('.msg').first()).toContainText('Poruka iz povijesti')
  await expect(bubble).not.toHaveClass(/rise/)
  await expect(page.locator('.newpill')).toHaveCount(0)
})

test('a reader scrolled up gets the New messages pill instead of being moved', async ({ page }) => {
  await openChat(page, 'Marta Dugi')
  await page.evaluate("document.querySelector('.msgs').scrollTop = 200")
  const top = await page.evaluate<number>("document.querySelector('.msgs').scrollTop")
  bridge.insert(MARTA, 'LIVE3', 'Nova poruka dok čitaš staro')
  const pill = page.getByRole('button', { name: 'New messages' })
  await expect(pill).toBeVisible({ timeout: 3000 })
  expect(Math.abs((await page.evaluate<number>("document.querySelector('.msgs').scrollTop")) - top)).toBeLessThanOrEqual(2)
  await pill.click()
  await expect(pill).toHaveCount(0)
  await expect(page.locator('.msg', { hasText: 'Nova poruka dok čitaš staro' })).toBeInViewport()
})

test('a message that arrived while the page was hidden shows once it is visible again', async ({ page }) => {
  await openChat(page, 'Lukas Brenner')
  const setVisibility = (state: string) =>
    page.evaluate(`(() => {
      Object.defineProperty(document, 'visibilityState', { value: '${state}', configurable: true })
      document.dispatchEvent(new Event('visibilitychange'))
    })()`)
  await setVisibility('hidden')
  bridge.insert(LUKAS, 'LIVE4', 'Poruka dok je stranica bila skrivena')
  await setVisibility('visible')
  await expect(page.locator('.msg', { hasText: 'Poruka dok je stranica bila skrivena' })).toBeVisible({ timeout: 3000 })
})

test('the chat list moves a chat to the top of the unpinned chats when it gets a message', async ({ page }) => {
  await signIn(page)
  const order = () => page.locator('.row .name').allTextContents()
  const names = await order()
  expect(names.indexOf('Lukas Brenner')).toBeGreaterThan(names.indexOf('Ekipa za vikend'))
  bridge.insert(LUKAS, 'LIVE5', 'Pomakni me na vrh')
  await expect(page.locator('.row', { hasText: 'Pomakni me na vrh' })).toBeVisible({ timeout: 3000 })
  const after = await order()
  expect(after.indexOf('Lukas Brenner')).toBeLessThan(after.indexOf('Ekipa za vikend'))
})

test('logging out ends the open live stream at once', async ({ page }) => {
  const ended = new Promise<void>((resolve) => {
    page.on('requestfinished', (r) => r.url().includes('/api/live') && !r.url().includes('visibility') && resolve())
    page.on('requestfailed', (r) => r.url().includes('/api/live') && !r.url().includes('visibility') && resolve())
  })
  const opened = page.waitForResponse((r) => r.url().endsWith('/api/live') && r.status() === 200)
  await signIn(page)
  await opened
  const status = await page.evaluate(async () => (await fetch('/api/auth/logout', { method: 'POST' })).status)
  expect(status).toBe(200)
  await Promise.race([ended, page.waitForTimeout(3000).then(() => Promise.reject(new Error('stream still open')))])
})
