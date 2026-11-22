import { expect, test } from '@playwright/test'
import { signIn } from './helpers.js'

async function openEkipa(page: import('@playwright/test').Page) {
  await signIn(page)
  await page.locator('.row .name', { hasText: 'Ekipa za vikend' }).click()
  await expect(page.locator('.msg').first()).toBeVisible()
}

test('media with no file shows a placeholder and Try again is rate limited', async ({ page }) => {
  const requests: string[] = []
  page.on('request', (r) => r.url().includes('/api/media/') && r.url().includes('E05') && requests.push(r.url()))
  await openEkipa(page)
  const photo = page.locator('.msg', { hasText: 'Ovo je ruta' }).locator('.photo')
  await expect(photo).toContainText('Media not available')
  expect(requests).toEqual([]) // rendering never asks for missing media
  await photo.getByRole('button', { name: 'Try again' }).click()
  // The server (and its 6 hour limit) is shared by the browser projects: the first tap may already be limited.
  await expect(photo.getByRole('status')).toHaveText(/Still not available|Asked recently, try again later/)
  await photo.getByRole('button', { name: 'Try again' }).click()
  await expect(photo.getByRole('status')).toHaveText('Asked recently, try again later')
  expect(requests.filter((u) => u.includes('retry=1'))).toHaveLength(2)
})

test('Show text on a voice note: error line, then the text', async ({ page }) => {
  await openEkipa(page)
  const voice = page.locator('.voice').first()
  await expect(voice).toContainText('0:37')
  await expect(voice.getByRole('button', { name: 'Play voice message' })).toBeVisible()
  // The test bridge is unreachable: the server answers with an error and the bubble says so.
  await voice.getByRole('button', { name: 'Show text' }).click()
  await expect(voice.getByRole('alert')).toHaveText('Audio not available')
  await page.route('**/api/transcripts/**', (route) =>
    route.fulfill({ json: { text: 'Vidimo se u subotu.', language: 'hr' } }),
  )
  await voice.getByRole('button', { name: 'Show text' }).click()
  await expect(voice.getByText('Vidimo se u subotu.')).toBeVisible()
  await voice.getByRole('button', { name: 'Hide text' }).click()
  await expect(voice.getByText('Vidimo se u subotu.')).toHaveCount(0)
})

test('voice note speed cycles 1x, 1.5x, 2x', async ({ page }) => {
  await openEkipa(page)
  const voice = page.locator('.voice').first()
  await voice.getByRole('button', { name: 'Playback speed 1×' }).click()
  await voice.getByRole('button', { name: 'Playback speed 1.5×' }).click()
  await expect(voice.getByRole('button', { name: 'Playback speed 2×' })).toBeVisible()
})
