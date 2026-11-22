import { expect, test, type Page } from '@playwright/test'
import { signIn } from './helpers.js'
import { forgetAppRead, stub } from './store.js'

const ANA = '385910000001@s.whatsapp.net'
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

test.beforeEach(async () => {
  await stub.reset()
})
test.afterEach(async () => {
  await stub.reset()
  stub.cleanup()
  forgetAppRead(ANA)
})

async function openAna(page: Page) {
  await signIn(page)
  await page.locator('.row .name', { hasText: 'Ana Kovačević' }).click()
  await expect(page.locator('.msg').first()).toBeVisible()
}

const sends = async () => (await stub.log()).filter((c) => c.path === '/api/send')

test('send: optimistic bubble, then confirmed by the live message without a duplicate', async ({ page }) => {
  await stub.delay(0.8)
  await openAna(page)
  const box = page.getByRole('textbox', { name: 'Message' })
  await expect(page.getByRole('button', { name: 'Send' })).toHaveCount(0) // no text: no send button (and no mic in M1)
  await expect(page.getByRole('button', { name: /Attach/ })).toBeDisabled()
  await box.fill('Vidimo se ')
  await box.press('Shift+Enter')
  await box.type('u sedam')
  await box.press('Enter')
  await expect(box).toHaveValue('')
  const bubble = page.locator('.msg.me', { hasText: 'u sedam' })
  await expect(bubble).toHaveClass(/sending/)
  await expect(bubble.getByText('Sending…')).toBeVisible()
  await expect(bubble).not.toHaveClass(/sending/, { timeout: 5000 })
  await expect(bubble.locator('time')).toBeVisible()
  await page.waitForTimeout(2500) // the live message for the same id arrives meanwhile
  await expect(page.locator('.msg.me', { hasText: 'u sedam' })).toHaveCount(1)
  const calls = await sends()
  expect(calls).toHaveLength(1)
  expect(calls[0].message).toBe('Vidimo se \nu sedam')
  expect(calls[0].recipient).toBe(ANA)
  expect(calls[0].idempotency_key).toMatch(UUID)
})

test('a 429 shows "Not sent: limit"; Retry sends with the same key and succeeds', async ({ page }) => {
  await openAna(page)
  await stub.failNext(429, 'rate_limited', 'slow down')
  const box = page.getByRole('textbox', { name: 'Message' })
  await box.fill('Stižem')
  await box.press('Enter')
  const bubble = page.locator('.msg.me', { hasText: 'Stižem' })
  await expect(bubble.getByRole('alert')).toContainText('Not sent: limit')
  await expect(bubble.getByText('Not sent', { exact: true })).toBeVisible()
  await bubble.getByRole('button', { name: 'Retry' }).click()
  await expect(bubble.getByRole('alert')).toHaveCount(0)
  await expect(bubble.locator('time')).toBeVisible()
  const calls = await sends()
  expect(calls).toHaveLength(2)
  expect(calls[1].idempotency_key).toBe(calls[0].idempotency_key)
  expect(calls[0].idempotency_key).toMatch(UUID)
  await page.waitForTimeout(2500)
  await expect(page.locator('.msg.me', { hasText: 'Stižem' })).toHaveCount(1)
})

test('a double click on Send sends once', async ({ page }) => {
  await stub.delay(0.5)
  await openAna(page)
  const box = page.getByRole('textbox', { name: 'Message' })
  await box.fill('Samo jednom')
  await page.getByRole('button', { name: 'Send' }).dblclick()
  await expect(page.locator('.msg.me', { hasText: 'Samo jednom' })).not.toHaveClass(/sending/, { timeout: 5000 })
  expect(await sends()).toHaveLength(1)
  await expect(page.locator('.msg.me', { hasText: 'Samo jednom' })).toHaveCount(1)
})

test('reply quotes the message and goes out with its id', async ({ page }) => {
  await openAna(page)
  const target = page.locator('.msg.them', { hasText: 'Ja sam slobodna u 7' })
  await target.hover()
  await target.getByRole('button', { name: 'Reply' }).click()
  await expect(page.locator('.replybar')).toContainText('Ja sam slobodna u 7')
  const box = page.getByRole('textbox', { name: 'Message' })
  await expect(box).toBeFocused()
  await box.fill('Super')
  await box.press('Enter')
  await expect(page.locator('.replybar')).toHaveCount(0)
  const bubble = page.locator('.msg.me', { hasText: 'Super' })
  await expect(bubble.locator('.quote')).toContainText('Ja sam slobodna u 7')
  await expect(bubble.locator('time')).toBeVisible()
  const calls = await sends()
  expect(calls).toHaveLength(1)
  expect(calls[0].quoted_message_id).toBeTruthy()
})

test('right click opens reactions: react, then take it back', async ({ page }) => {
  await openAna(page)
  const target = page.locator('.msg.them', { hasText: 'Ja sam slobodna u 7' })
  await target.locator('.bubble').click({ button: 'right' })
  const menu = page.getByRole('dialog', { name: 'Message actions' })
  await expect(menu.getByRole('button', { name: /^React / })).toHaveCount(6)
  await menu.getByRole('button', { name: 'React 👍' }).click()
  await expect(menu).toHaveCount(0)
  await expect(target.getByLabel('1 👍, including you')).toBeVisible()
  await expect.poll(async () => (await stub.log()).filter((c) => c.path === '/api/reaction').length).toBe(1)
  await target.locator('.bubble').click({ button: 'right' })
  await page.getByRole('button', { name: 'Remove 👍' }).click()
  await expect(target.getByLabel(/👍/)).toHaveCount(0)
  await expect
    .poll(async () => (await stub.log()).filter((c) => c.path === '/api/reaction').map((r) => r.emoji))
    .toEqual(['👍', ''])
})
