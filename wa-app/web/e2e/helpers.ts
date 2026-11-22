import { expect, type Page } from '@playwright/test'

export const PASSPHRASE = 'correct horse battery'

/** Opens the app signed in: first run sets the passphrase, later runs log in. */
export async function signIn(page: Page): Promise<void> {
  await page.goto('/')
  await expect(page).toHaveURL(/\/(login)?$/)
  if (!page.url().endsWith('/login')) return
  const repeat = page.getByLabel('Repeat passphrase')
  if (await repeat.isVisible()) {
    await page.getByLabel('Passphrase', { exact: true }).fill(PASSPHRASE)
    await repeat.fill(PASSPHRASE)
    await page.getByRole('button', { name: 'Set passphrase' }).click()
  } else {
    await page.getByLabel('Passphrase').fill(PASSPHRASE)
    await page.getByRole('button', { name: 'Log in' }).click()
  }
  await expect(page.getByRole('heading', { name: 'Chats' })).toBeVisible()
}
