import { expect, test } from '@playwright/test'

const PASSPHRASE = 'correct horse battery'

// One server and one data dir serve both browser projects, so the steps run as one ordered flow:
// whichever project goes first performs the first-run setup, the other one just logs in.
test('setup or login, reload keeps the session, logout returns to login', async ({ page }) => {
  await page.goto('/')
  await expect(page).toHaveURL(/\/login$/)

  const repeat = page.getByLabel('Repeat passphrase')
  if (await repeat.isVisible()) {
    await page.getByLabel('Passphrase', { exact: true }).fill(PASSPHRASE)
    await repeat.fill(PASSPHRASE)
    await page.getByRole('button', { name: 'Set passphrase' }).click()
  } else {
    await page.getByLabel('Passphrase').fill('definitely wrong')
    await page.getByRole('button', { name: 'Log in' }).click()
    await expect(page.getByRole('alert')).toHaveText('Wrong passphrase')
    await page.getByLabel('Passphrase').fill(PASSPHRASE)
    await page.getByRole('button', { name: 'Log in' }).click()
  }
  await expect(page.getByRole('heading', { name: 'Chats' })).toBeVisible()

  await page.reload()
  await expect(page.getByRole('heading', { name: 'Chats' })).toBeVisible()

  await page.getByRole('link', { name: 'Settings' }).click()
  await page.getByRole('button', { name: 'Log out' }).click()
  await expect(page).toHaveURL(/\/login$/)
  await page.reload()
  await expect(page.getByRole('button', { name: 'Log in' })).toBeVisible()
})
