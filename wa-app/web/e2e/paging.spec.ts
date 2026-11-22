import { expect, test } from '@playwright/test'
import { signIn } from './helpers.js'

test('older messages load once and the reader keeps their place', async ({ page }) => {
  const pages: string[] = []
  page.on('request', (r) => {
    if (r.url().includes('/messages')) pages.push(r.url())
  })
  await signIn(page)
  await page.locator('.row .name', { hasText: 'Marta Dugi' }).click()
  await expect(page.locator('.msg').last()).toContainText('Filler message 150')
  await expect(page.locator('.msg')).toHaveCount(60)
  expect(pages).toHaveLength(1)

  // Scroll into the load-older zone and note, in the same synchronous step (before the scroll event runs), which
  // message is at the top of the view and where it sits in the pane.
  const response = page.waitForResponse((r) => r.url().includes('before='))
  const before = (await page.evaluate(`(() => {
    const scroller = document.querySelector('.msgs')
    scroller.scrollTop = 120
    const pane = scroller.getBoundingClientRect()
    const el = [...document.querySelectorAll('.msg')].find((m) => m.getBoundingClientRect().top >= pane.top)
    return { id: el.dataset.messageId, text: el.textContent, offset: el.getBoundingClientRect().top - pane.top }
  })()`)) as { id: string; text: string; offset: number }
  await response
  await expect(page.locator('.msg')).toHaveCount(120)

  const after = (await page.evaluate(`(() => {
    const pane = document.querySelector('.msgs').getBoundingClientRect()
    return document.querySelector('[data-message-id="${before.id}"]').getBoundingClientRect().top - pane.top
  })()`)) as number
  expect(Math.abs(after - before.offset)).toBeLessThanOrEqual(4)
  expect(before.text).toContain('Filler message')

  await page.waitForTimeout(800) // a cascade, if any, would have fetched the rest by now
  expect(pages.filter((u) => u.includes('before='))).toHaveLength(1)
  await expect(page.locator('.msg')).toHaveCount(120)
})

test('a failed older-page request can be retried by scrolling again', async ({ page }) => {
  let failed = 0
  await signIn(page)
  await page.locator('.row .name', { hasText: 'Marta Dugi' }).click()
  await expect(page.locator('.msg')).toHaveCount(60)
  // Installed once the chat is open: WebKit stalls the chat list when a route is active during sign-in.
  await page.route('**/messages?before=*', async (route) => {
    if (failed === 0) {
      failed += 1
      await route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"boom"}' })
    } else await route.continue()
  })
  await page.evaluate("document.querySelector('.msgs').scrollTop = 120")
  await expect.poll(() => failed).toBe(1) // the route answered the first request with a 500
  await expect(page.locator('.older')).toHaveCount(0)
  await expect(page.locator('.msg')).toHaveCount(60)

  await page.evaluate("document.querySelector('.msgs').scrollTop = 130") // a new scroll event
  await expect(page.locator('.msg')).toHaveCount(120)
  expect(failed).toBe(1)
})
