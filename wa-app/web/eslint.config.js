import js from '@eslint/js'
import reactHooks from 'eslint-plugin-react-hooks'
import react from 'eslint-plugin-react'
import globals from 'globals'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  { ignores: ['dist', 'playwright-report', 'test-results'] },
  {
    files: ['**/*.{ts,tsx}'],
    extends: [js.configs.recommended, ...tseslint.configs.recommended, reactHooks.configs.flat.recommended],
    plugins: { react },
    // Message text is untrusted: it is only ever rendered as React text nodes.
    rules: { 'react/no-danger': 'error', 'react/no-danger-with-children': 'error' },
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
  },
)
