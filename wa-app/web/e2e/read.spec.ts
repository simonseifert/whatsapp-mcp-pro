import { expect, test } from '@playwright/test'
import { signIn } from './helpers.js'
import { FakeBridge, forgetAppRead } from './store.js'

const ANA = '385910000001@s.whatsapp.net'
let bridge: FakeBridge
test.beforeEach(() => {
  bridge = new FakeBridge()
})
test.afterEach(() => {
  bridge.cleanup()
  forgetAppRead(ANA)
})

test('opening an unread chat marks it read: the badge goes and stays gone', async ({ page }) => {
  await signIn(page)
  await expect(page.getByLabel('2 unread', { exact: true })).toBeVisible()
  const read = page.waitForResponse((r) => r.url().includes('/read') && r.request().method() === 'POST')
  await page.locator('.row .name', { hasText: 'Ana Kovačević' }).click()
  expect((await read).ok()).toBe(true)
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Chats' })).toBeVisible()
  await expect(page.locator('.row', { hasText: 'Ana Kovačević' }).locator('.badge')).toHaveCount(0)
  await expect(page.getByLabel('2 unread', { exact: true })).toHaveCount(0)
})

test('a message arriving in the open, visible chat is marked read too', async ({ page }) => {
  await signIn(page)
  await page.locator('.row .name', { hasText: 'Ana Kovačević' }).click()
  await expect(page.locator('.msg').first()).toBeVisible()
  const read = page.waitForResponse((r) => r.url().includes('/read') && r.request().method() === 'POST')
  bridge.insert(ANA, 'LIVE-R', 'Stigla sam')
  await expect(page.locator('.msg', { hasText: 'Stigla sam' })).toBeVisible({ timeout: 3000 })
  expect((await read).ok()).toBe(true)
})
