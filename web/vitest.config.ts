import { execFileSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { defineVitestConfig } from '@nuxt/test-utils/config'

// tsconfig.json extends the generated .nuxt/tsconfig.json and Vite 8 (oxc) fails
// every .ts transform when it is missing. The nuxt test environment does not write
// it, so a clean checkout would fail without this.
const root = fileURLToPath(new URL('.', import.meta.url))
if (!existsSync(fileURLToPath(new URL('.nuxt/tsconfig.json', import.meta.url)))) {
  execFileSync('npx', ['nuxt', 'prepare'], { cwd: root, stdio: 'inherit' })
}

export default defineVitestConfig({
  test: {
    environment: 'nuxt',
    include: ['tests/**/*.test.ts'],
  },
})
