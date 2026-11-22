import { expect, test, type Page } from '@playwright/test'
import { signIn } from './helpers.js'

const noHorizontalScroll = (page: Page) =>
  page.evaluate<boolean>('document.documentElement.scrollWidth <= window.innerWidth')

async function openChat(page: Page, name: string) {
  await signIn(page)
  await page.locator('.row .name', { hasText: name }).click()
  await expect(page).toHaveURL(/\/chat\//)
  await expect(page.locator('.msg').first()).toBeVisible()
}

test('Ekipa za vikend shows every message kind', async ({ page }) => {
  await openChat(page, 'Ekipa za vikend')
  await expect(page.getByRole('heading', { name: 'Ekipa za vikend' })).toBeVisible()
  await expect(page.locator('.mem')).toContainText('Nikola')

  // day separators
  await expect(page.locator('.day').first()).toHaveText(/Today|Yesterday|[A-Z][a-z]+day|\d{4}/)
  // plain, edited, forwarded, deleted
  await expect(page.getByText('Tko vozi u subotu?')).toBeVisible()
  const edited = page.locator('.msg.me', { hasText: 'krećemo oko 9' })
  await expect(edited.getByText('Edited')).toBeVisible()
  const forwarded = page.locator('.msg', { hasText: 'Prognoza za Učku' })
  await expect(forwarded.getByText('Forwarded')).toBeVisible()
  await expect(forwarded.getByLabel('3 ☀️, including you')).toBeVisible()
  await expect(page.locator('.bubble.deleted')).toContainText('Message deleted')
  await expect(page.getByText('gone', { exact: true })).toHaveCount(0) // a deleted message's text never reaches the page
  // sender names in a group
  await expect(page.locator('.sender', { hasText: 'Petra' }).first()).toBeVisible()
  // photo with caption and reactions
  const photo = page.locator('.msg', { hasText: 'Ovo je ruta' })
  await expect(photo.locator('.photo')).toBeVisible()
  await expect(photo.getByLabel('2 🔥')).toBeVisible()
  await expect(photo.getByLabel('1 👍')).toBeVisible()
  // poll
  const poll = page.locator('.poll')
  await expect(poll.getByText('Ručak poslije?')).toBeVisible()
  await expect(poll.locator('.opt', { hasText: 'Konoba kod Mate' })).toContainText('3')
  await expect(poll.getByText('4 votes')).toBeVisible()
  // voice note (placeholder until the media step) with its length
  await expect(page.getByText('Voice message · 0:37')).toBeVisible()
  // reply quote, including a quote of a deleted message
  await expect(page.locator('.quote', { hasText: 'Voice message (0:37)' })).toContainText('Petra')
  await expect(page.locator('.quote', { hasText: 'Message deleted' })).toBeVisible()
  await expect(page.getByText('old quoted text')).toHaveCount(0)
  // contact card
  const card = page.locator('.contact');
  await expect(card).toContainText('Ivo Horvat')
  await expect(card.getByRole('link', { name: '+385 91 000 0777' })).toHaveAttribute('href', 'tel:+385910000777')
  // link
  const link = page.getByRole('link', { name: 'https://example.org/ruta?x=1' })
  await expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  await expect(link).toHaveAttribute('target', '_blank')
  // system note
  await expect(page.locator('.sysmsg')).toContainText('promijenila naziv grupe')
  // opens at the newest message
  await expect(page.locator('.sysmsg')).toBeInViewport()
  expect(await noHorizontalScroll(page)).toBe(true)
})

test('markup in a message shows literally and nothing runs', async ({ page }) => {
  let dialogs = 0
  page.on('dialog', (d) => {
    dialogs += 1
    void d.dismiss()
  })
  await openChat(page, 'Ekipa za vikend')
  await expect(page.getByText('<b>hi</b> <script>alert(1)</script>')).toBeVisible()
  await expect(page.locator('.txt b, .txt script')).toHaveCount(0)
  expect(dialogs).toBe(0)
})

for (const width of [320, 375]) {
  test(`a 5,000-character message wraps without horizontal scroll at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 })
    await openChat(page, 'Lukas Brenner')
    const long = page.locator('.txt', { hasText: 'Lang: xxxx' })
    await long.scrollIntoViewIfNeeded()
    await expect(long).toBeVisible()
    expect(await noHorizontalScroll(page)).toBe(true)
    const box = await long.boundingBox()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(width) // wrapped inside the bubble, not running off
    expect(box!.height).toBeGreaterThan(200) // 5,000 characters take many lines
    expect(await page.evaluate("getComputedStyle(document.querySelector('.msgs')).overflowX")).toBe('hidden')
  })
}

test('both halves of a split person show in one chat, in time order', async ({ page }) => {
  await openChat(page, 'Tomo (posao)')
  await expect(page.locator('.msg .txt')).toHaveText(['Pozdrav iz Njemačke', 'Stigao sam'])
})

test('back to the list on a phone; list stays beside the chat from 900px', async ({ page }) => {
  await openChat(page, 'Ekipa za vikend')
  await page.getByRole('link', { name: 'Back to chats' }).click()
  await expect(page.getByRole('heading', { name: 'Chats' })).toBeVisible()
  await page.setViewportSize({ width: 1100, height: 800 })
  await page.locator('.row .name', { hasText: 'Ekipa za vikend' }).click()
  await expect(page.locator('.row').first()).toBeVisible()
  await expect(page.locator('.chatpane')).toBeVisible()
  await expect(page.getByRole('link', { name: 'Back to chats' })).toBeHidden()
  expect(await noHorizontalScroll(page)).toBe(true)
})

test('a chat that does not exist says so', async ({ page }) => {
  await signIn(page)
  await page.goto('/chat/999%40s.whatsapp.net')
  await expect(page.getByText('This chat does not exist.')).toBeVisible()
})
