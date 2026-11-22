import { defineConfig, devices } from '@playwright/test'

const port = Number(process.env.WA_APP_PORT ?? 18085)

export default defineConfig({
  testDir: './e2e',
  workers: 1,
  use: { baseURL: `http://127.0.0.1:${port}` },
  webServer: {
    command: './e2e/serve.sh',
    url: `http://127.0.0.1:${port}/api/health`,
    reuseExistingServer: false,
    env: { WA_APP_PORT: String(port) },
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'], viewport: { width: 375, height: 812 } } },
    { name: 'webkit', use: { ...devices['Desktop Safari'], viewport: { width: 375, height: 812 } } },
  ],
})
