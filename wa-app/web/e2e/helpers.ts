import { expect, type Page } from '@playwright/test'

export const PASSPHRASE = 'correct horse battery'

/** Opens the app signed in: first run sets the passphrase, later runs log in. */
export async function signIn(page: Page): Promise<void> {
  await page.goto('/')
  // The app first loads its auth state and only then redirects, so '/' is not yet proof of a session: wait until
  // either the chat list or the passphrase form is on screen.
  const chats = page.getByRole('heading', { name: 'Chats' })
  const passphrase = page.getByLabel('Passphrase', { exact: true })
  await expect(chats.or(passphrase)).toBeVisible()
  if (await chats.isVisible()) return
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
