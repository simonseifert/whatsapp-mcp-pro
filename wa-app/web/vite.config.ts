import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import type { Plugin } from 'vite'
import { defineConfig } from 'vitest/config'

// Files in public/ that belong to the shell (they are not part of the bundle graph, so they are listed here).
const PUBLIC_SHELL = ['/favicon.svg', '/favicon.ico', '/apple-touch-icon.png', '/icon-192.png', '/icon-512.png', '/site.webmanifest']
const SHELL_EXT = /\.(js|css|woff2?|png|svg|ico)$/

/** Builds src/sw.ts to /sw.js (no hash in its name, the browser polls that URL) and fills in its shell list and version. */
function serviceWorker(): Plugin {
  return {
    name: 'wa-service-worker',
    apply: 'build',
    config: () => ({
      build: {
        rollupOptions: {
          input: { main: 'index.html', sw: 'src/sw.ts' },
          output: { entryFileNames: (chunk) => (chunk.name === 'sw' ? 'sw.js' : 'assets/[name]-[hash].js') },
        },
      },
    }),
    generateBundle(_options, bundle) {
      const files = Object.keys(bundle).filter((name) => name !== 'sw.js' && SHELL_EXT.test(name))
      const shell = ['/', ...files.map((name) => `/${name}`), ...PUBLIC_SHELL]
      // The version covers the file names and the bytes of the public files (icons, manifest), which are not hashed names.
      const digest = createHash('sha256').update(shell.join('\n'))
      for (const name of PUBLIC_SHELL) digest.update(readFileSync(`public${name}`))
      const hash = digest.digest('hex').slice(0, 12)
      const sw = bundle['sw.js']
      if (sw?.type !== 'chunk') throw new Error('sw.js was not built')
      sw.code = sw.code.replace('__BUILD_HASH__', JSON.stringify(hash)).replace('__SHELL_FILES__', JSON.stringify(shell))
    },
  }
}

export default defineConfig({
  plugins: [react(), tailwindcss(), serviceWorker()],
  server: { proxy: { '/api': 'http://127.0.0.1:8085' } },
  test: { environment: 'jsdom', include: ['src/**/*.test.{ts,tsx}'], globals: false },
})
